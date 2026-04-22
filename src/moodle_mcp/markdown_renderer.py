"""Obsidian-friendly Markdown renderers for the v2.1 per-module layout.

Three levels, each with its own `.md`:

- :func:`render_course_overview` → ``Kurs.md`` in the course root
- :func:`render_section_overview` → ``Section.md`` in each section folder
- :func:`render_module` → ``<module>.md`` inside each module folder

All cross-file links are relative to the file where they appear, so the
tree works as a self-contained Obsidian vault.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from .html_utils import html_to_plaintext
from .paths import ASSIGNMENTS_GROUP_DIR, INFOTEXTS_GROUP_DIR, classify_module_group


def _format_duedate(timestamp: Any) -> Optional[str]:
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _rel_link(from_file: Path, to_target: Path) -> str:
    """Return a url-safe relative Markdown link from one file to a target path."""
    try:
        rel = Path(to_target).relative_to(from_file.parent)
    except ValueError:
        # Fall back to an absolute POSIX path so the link is at least usable.
        rel = Path(to_target)
    return quote(rel.as_posix(), safe="/-._~()")


def _yaml_escape(value: str) -> str:
    """Quote a YAML string value if needed."""
    if not value:
        return '""'
    if any(c in value for c in ':#\n"\\'):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def _yaml_frontmatter(fields: dict[str, Any]) -> list[str]:
    lines = ["---"]
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, list):
            joined = ", ".join(str(v) for v in value)
            lines.append(f"{key}: [{joined}]")
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, (int, float)):
            lines.append(f"{key}: {value}")
        else:
            lines.append(f"{key}: {_yaml_escape(str(value))}")
    lines.append("---")
    return lines


# ---------------------------------------------------------------- course overview
def render_course_overview(
    course: dict[str, Any],
    category_name: Optional[str],
    sections_with_paths: list[tuple[dict[str, Any], Path]],
    kurs_md_path: Path,
    retrieved_at: Optional[datetime] = None,
) -> str:
    retrieved = retrieved_at or datetime.now(timezone.utc)
    fullname = course.get("fullname") or course.get("shortname") or "Kurs"

    lines = _yaml_frontmatter({
        "type": "moodle-course",
        "course_id": course.get("id"),
        "shortname": course.get("shortname"),
        "category": category_name,
        "retrieved": retrieved.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tags": ["moodle"],
    })
    lines.append("")
    lines.append(f"# {fullname}")
    lines.append("")

    meta = []
    if course.get("id") is not None:
        meta.append(f"**Kurs-ID:** {course['id']}")
    if course.get("shortname"):
        meta.append(f"**Shortname:** {course['shortname']}")
    if category_name:
        meta.append(f"**Kategorie:** {category_name}")
    meta.append(f"**Abgerufen:** {retrieved.strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("  \n".join(meta))
    lines.append("")

    summary = html_to_plaintext(course.get("summary") or "")
    if summary:
        lines.append(summary)
        lines.append("")

    if sections_with_paths:
        lines.append("## Sections")
        lines.append("")
        for section, section_md_path in sections_with_paths:
            name = section.get("name") or "(ohne Titel)"
            link = _rel_link(kurs_md_path, section_md_path)
            lines.append(f"- [{name}]({link})")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------- section overview
def render_section_overview(
    course: dict[str, Any],
    section: dict[str, Any],
    section_index: int,
    modules_with_paths: list[tuple[dict[str, Any], dict[str, Any] | None, Path]],
    section_md_path: Path,
    retrieved_at: Optional[datetime] = None,
) -> str:
    """Render Section.md listing every module linked to its own .md file.

    ``modules_with_paths`` items: ``(module_dict, assign_meta_or_None, module_md_path)``.
    """
    retrieved = retrieved_at or datetime.now(timezone.utc)
    name = section.get("name") or f"Section {section_index}"

    lines = _yaml_frontmatter({
        "type": "moodle-section",
        "course_id": course.get("id"),
        "section_index": section_index,
        "retrieved": retrieved.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tags": ["moodle", "section"],
    })
    lines.append("")
    lines.append(f"# {name}")
    lines.append("")

    summary = html_to_plaintext(section.get("summary") or "")
    if summary:
        lines.append(summary)
        lines.append("")

    assignments = [
        (m, a, p) for (m, a, p) in modules_with_paths
        if classify_module_group(m.get("modname")) == ASSIGNMENTS_GROUP_DIR
    ]
    infotexts = [
        (m, a, p) for (m, a, p) in modules_with_paths
        if classify_module_group(m.get("modname")) == INFOTEXTS_GROUP_DIR
    ]

    if assignments:
        lines.append(f"## {ASSIGNMENTS_GROUP_DIR}")
        lines.append("")
        for module, assign_meta, md_path in assignments:
            mname = module.get("name") or "(unbenannt)"
            link = _rel_link(section_md_path, md_path)
            due = _format_duedate((assign_meta or {}).get("duedate"))
            suffix = f" — fällig {due}" if due else ""
            lines.append(f"- [{mname}]({link}){suffix}")
        lines.append("")

    if infotexts:
        lines.append(f"## {INFOTEXTS_GROUP_DIR}")
        lines.append("")
        for module, _assign, md_path in infotexts:
            mname = module.get("name") or "(unbenannt)"
            modname = module.get("modname") or "unknown"
            link = _rel_link(section_md_path, md_path)
            lines.append(f"- [{mname}]({link}) `[{modname}]`")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------- module
def render_module(
    course: dict[str, Any],
    section: dict[str, Any],
    module: dict[str, Any],
    assign_meta: Optional[dict[str, Any]],
    module_md_path: Path,
    attachment_paths: list[Path],
    retrieved_at: Optional[datetime] = None,
) -> str:
    retrieved = retrieved_at or datetime.now(timezone.utc)
    name = module.get("name") or "(unbenannt)"
    modname = module.get("modname") or "unknown"
    is_assign = classify_module_group(modname) == ASSIGNMENTS_GROUP_DIR

    frontmatter = {
        "type": "moodle-module",
        "modtype": modname,
        "course_id": course.get("id"),
        "section": section.get("name"),
        "retrieved": retrieved.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tags": ["moodle", modname],
    }
    if is_assign and assign_meta:
        frontmatter["assign_id"] = assign_meta.get("id")
        due = _format_duedate(assign_meta.get("duedate"))
        if due:
            frontmatter["duedate"] = due

    lines = _yaml_frontmatter(frontmatter)
    lines.append("")
    lines.append(f"# {name}")
    lines.append("")
    lines.append(f"`[{modname}]`")
    lines.append("")

    if is_assign and assign_meta:
        due = _format_duedate(assign_meta.get("duedate"))
        if due:
            lines.append(f"**Fällig:** {due}")
            lines.append("")

    description_html = module.get("description")
    if is_assign and assign_meta:
        description_html = assign_meta.get("intro") or description_html
    description = html_to_plaintext(description_html or "")
    if description:
        lines.append(description)
        lines.append("")

    if modname == "url":
        for item in module.get("contents") or []:
            if isinstance(item, dict):
                url = item.get("fileurl") or item.get("url")
                if url:
                    lines.append(f"**Externer Link:** {url}")
                    lines.append("")

    if attachment_paths:
        lines.append("## Anhänge")
        lines.append("")
        for path in attachment_paths:
            link = _rel_link(module_md_path, path)
            lines.append(f"- [{path.name}]({link})")
        lines.append("")

    if is_assign:
        lines.append("## Abgabe")
        lines.append("")
        lines.append(
            "Lege Dateien für deine Abgabe im Ordner `Abgabe/` neben dieser "
            "Datei ab. `submit_assignment` löst relative Pfade gegen diesen "
            "Ordner auf."
        )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
