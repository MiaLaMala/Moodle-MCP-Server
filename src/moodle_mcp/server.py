"""FastMCP server exposing ``list_courses`` and ``get_course_content`` tools."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from .config import MoodleConfig
from .downloader import download_course as run_download_course
from .html_utils import html_to_plaintext
from .moodle_client import MoodleAPIError, MoodleAuthError, MoodleClient
from .submissions import (
    get_submission_status as run_get_submission_status,
    get_upcoming_deadlines as run_get_upcoming_deadlines,
    submit_assignment as run_submit_assignment,
)


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

    @mcp.tool()
    async def download_course(course_id: int) -> str:
        """Lade den gesamten Kurs (Text + alle Anhänge) in den lokalen Dokumente-Ordner.

        Legt folgende Struktur an:
            <MOODLE_DOWNLOAD_ROOT>/<host>/<Kategorie>/<Kurs>/<Kurs>.md
                                                      /Anhänge/<Section>/<Modul>/…
                                                      /Abgaben/   (leer, für Submit-Files)

        Die erzeugte .md ist Obsidian-freundlich (YAML-Frontmatter, relative Links).
        Bereits vorhandene Dateien mit passender Größe werden übersprungen.

        Args:
            course_id: die numerische Moodle-Kurs-ID (siehe list_courses).
        """
        try:
            client = await get_client()
            manifest = await run_download_course(
                client=client,
                course_id=course_id,
                download_root=config.download_root,
                moodle_url=config.url or "",
            )
        except (MoodleAuthError, MoodleAPIError) as err:
            return f"Fehler: {err}"

        lines = [
            f"# Download — {manifest.course_name}",
            "",
            f"- Kurs-ID: **{manifest.course_id}**",
            f"- Kurs-Ordner: `{manifest.course_dir}`",
            f"- Markdown: `{manifest.markdown_path.name}`",
            f"- Neu heruntergeladen: **{len(manifest.downloaded)}** Dateien "
            f"({manifest.total_bytes} Bytes)",
            f"- Übersprungen (bereits aktuell): **{len(manifest.skipped)}**",
        ]
        if manifest.failed:
            lines.append(f"- Fehlgeschlagen: **{len(manifest.failed)}**")
            for fpath, err in manifest.failed[:10]:
                lines.append(f"  - {fpath}: {err}")
        return "\n".join(lines)

    @mcp.tool()
    async def submit_assignment(
        course_id: int,
        assign_id: int,
        text: Optional[str] = None,
        file_paths: Optional[list[str]] = None,
        i_confirm: bool = False,
        final: bool = False,
    ) -> str:
        """Reicht eine Aufgabe in Moodle ein.

        ⚠️  WICHTIG — destruktiv / kaum reversibel. Verhalten nach `i_confirm`:

        - `i_confirm=False` (Default): **DRY RUN** — zeigt nur, was eingereicht würde.
        - `i_confirm=True, final=False`: speichert als **Draft** in Moodle (reversibel).
        - `i_confirm=True, final=True`: ruft **mod_assign_submit_for_grading** auf.

        Niemals ohne ausdrückliche Bestätigung des Users mit `i_confirm=True` aufrufen.

        Args:
            course_id: die Kurs-ID (für Pfad-Auflösung der file_paths).
            assign_id: die numerische Aufgaben-ID (aus mod_assign, siehe Kurs-Inhalt).
            text: optionaler Online-Text (Plaintext; wird zu HTML konvertiert).
            file_paths: optionale Datei-Pfade. Absolute Pfade werden direkt genommen,
                relative Pfade werden gegen `<Kurs-Ordner>/Abgaben/` aufgelöst.
            i_confirm: EXPLIZIT auf True setzen, um wirklich einzureichen.
            final: zusätzlich auf True für finales Abgeben (`submit_for_grading`).
        """
        try:
            client = await get_client()
            return await run_submit_assignment(
                client=client,
                config=config,
                course_id=course_id,
                assign_id=assign_id,
                text=text,
                file_paths=file_paths,
                i_confirm=i_confirm,
                final=final,
            )
        except (MoodleAuthError, MoodleAPIError) as err:
            return f"Fehler: {err}"

    @mcp.tool()
    async def get_submission_status(assign_id: int) -> str:
        """Zeigt Abgabestatus einer Aufgabe: hast du eingereicht, Note, Lehrer-Feedback.

        Args:
            assign_id: die numerische Aufgaben-ID.
        """
        try:
            client = await get_client()
            return await run_get_submission_status(client, assign_id)
        except (MoodleAuthError, MoodleAPIError) as err:
            return f"Fehler: {err}"

    @mcp.tool()
    async def get_upcoming_deadlines(days: int = 14) -> str:
        """Zeigt alle fälligen Aufgaben quer über alle Kurse, sortiert nach Deadline.

        Args:
            days: Zeitfenster in Tagen (Default 14).
        """
        try:
            client = await get_client()
            return await run_get_upcoming_deadlines(client, days=days)
        except (MoodleAuthError, MoodleAPIError) as err:
            return f"Fehler: {err}"

    return mcp
