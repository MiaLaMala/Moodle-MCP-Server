"""FastMCP server exposing ``list_courses`` and ``get_course_content`` tools."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from .config import MoodleConfig
from .html_utils import html_to_plaintext
from .moodle_client import MoodleAPIError, MoodleAuthError, MoodleClient


_ASSIGN_MODNAMES = {"assign"}
_TEXT_MODNAMES = {"page", "label", "book", "resource", "url"}


def _format_duedate(timestamp: Any) -> Optional[str]:
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _format_courses(courses: list[dict[str, Any]]) -> str:
    if not courses:
        return "Keine Kurse gefunden."
    lines = ["# Kurse", ""]
    for c in courses:
        cid = c.get("id")
        shortname = c.get("shortname") or "?"
        fullname = c.get("fullname") or "?"
        category = c.get("category")
        cat = f" (Kategorie: {category})" if category is not None else ""
        lines.append(f"- [{cid}] {shortname} — {fullname}{cat}")
    return "\n".join(lines)


def _format_module(module: dict[str, Any], assign_by_cmid: dict[int, dict[str, Any]]) -> str:
    modname = module.get("modname") or "unknown"
    name = module.get("name") or "(unbenannt)"
    header = f"### [{modname}] {name}"
    parts = [header]

    if modname in _ASSIGN_MODNAMES:
        cmid = module.get("id")
        assign_meta = assign_by_cmid.get(int(cmid)) if cmid is not None else None
        if assign_meta:
            due = _format_duedate(assign_meta.get("duedate"))
            if due:
                parts.append(f"Fällig: {due}")
            intro_html = assign_meta.get("intro") or module.get("description") or ""
            intro = html_to_plaintext(intro_html)
            if intro:
                parts.append(intro)
            return "\n".join(parts)

    description = html_to_plaintext(module.get("description") or "")
    if description:
        parts.append(description)

    # Some modules (resource/url/…) list files under `contents`.
    contents = module.get("contents") or []
    links = []
    for item in contents:
        if not isinstance(item, dict):
            continue
        url = item.get("fileurl") or item.get("url")
        filename = item.get("filename") or item.get("name") or url
        if url:
            links.append(f"- {filename}: {url}")
    if links:
        parts.append("Anhänge:\n" + "\n".join(links))

    if len(parts) == 1:
        parts.append("(keine Beschreibung)")
    return "\n".join(parts)


def _format_course_content(
    sections: list[dict[str, Any]],
    assignments: list[dict[str, Any]],
) -> str:
    if not sections:
        return "Keine Inhalte für diesen Kurs gefunden."

    assign_by_cmid: dict[int, dict[str, Any]] = {}
    for a in assignments:
        cmid = a.get("cmid")
        if cmid is not None:
            try:
                assign_by_cmid[int(cmid)] = a
            except (TypeError, ValueError):
                continue

    out: list[str] = []
    for section in sections:
        section_name = section.get("name") or "(ohne Titel)"
        out.append(f"## Section: {section_name}")
        summary = html_to_plaintext(section.get("summary") or "")
        if summary:
            out.append(summary)

        modules = section.get("modules") or []
        if not modules:
            out.append("(keine Module)")
            out.append("")
            continue

        for module in modules:
            if not module.get("visible", 1):
                continue
            out.append(_format_module(module, assign_by_cmid))
            out.append("")

        out.append("")

    return "\n".join(out).rstrip() + "\n"


def create_server(config: MoodleConfig) -> FastMCP:
    """Build the FastMCP app bound to a concrete config.

    The Moodle client is created lazily on first tool invocation so that a
    server can start (and advertise tools) even while the Moodle host is
    temporarily unreachable.
    """
    mcp = FastMCP("moodle-mcp")
    state: dict[str, Optional[MoodleClient]] = {"client": None}

    async def get_client() -> MoodleClient:
        if state["client"] is None:
            state["client"] = MoodleClient(config)
        return state["client"]

    @mcp.tool()
    async def list_courses() -> str:
        """Liste alle Moodle-Kurse, in denen du eingeschrieben bist.

        Gibt pro Kurs id, shortname, fullname und category zurück.
        """
        try:
            client = await get_client()
            courses = await client.list_courses()
        except (MoodleAuthError, MoodleAPIError) as err:
            return f"Fehler: {err}"
        return _format_courses(courses)

    @mcp.tool()
    async def get_course_content(course_id: int) -> str:
        """Gib alle Aufgaben + Infotexte eines Kurses als strukturierten Text aus.

        Args:
            course_id: die numerische Moodle-Kurs-ID (siehe list_courses).
        """
        try:
            client = await get_client()
            sections = await client.get_course_contents(course_id)
            assignments = await client.get_assignments(course_id)
        except (MoodleAuthError, MoodleAPIError) as err:
            return f"Fehler: {err}"
        return _format_course_content(sections, assignments)

    return mcp
