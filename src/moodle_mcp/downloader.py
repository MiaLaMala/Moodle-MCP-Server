"""Orchestrates the full ``download_course`` flow:

- Resolves course + category (folder name "Lernfeld 3").
- Walks every section/module, downloads every attachment to Anhänge/<section>/.
- Creates Abgaben/ (empty) for the user to drop submission files in.
- Writes an Obsidian-friendly Markdown summary with relative links.
- Skips files already on disk if their size matches (incremental sync).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .markdown_renderer import render_course_markdown
from .moodle_client import MoodleAPIError, MoodleClient
from .paths import (
    SUBMISSIONS_DIR,
    attachments_subdir,
    build_course_dir,
    sanitize_path_component,
    submissions_dir,
)


logger = logging.getLogger("moodle_mcp.downloader")


@dataclass
class DownloadManifest:
    course_id: int
    course_name: str
    course_dir: Path
    markdown_path: Path
    downloaded: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    total_bytes: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "course_id": self.course_id,
            "course_name": self.course_name,
            "course_dir": str(self.course_dir),
            "markdown_path": str(self.markdown_path),
            "downloaded_count": len(self.downloaded),
            "skipped_count": len(self.skipped),
            "failed_count": len(self.failed),
            "total_bytes": self.total_bytes,
            "failed": [{"file": name, "error": err} for name, err in self.failed],
        }


def _collect_attachments(
    module: dict[str, Any],
    assign_meta: Optional[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge attachment descriptors from the module and (for assigns) intro files."""
    items: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    def add(item: Any) -> None:
        if not isinstance(item, dict):
            return
        itype = item.get("type")
        url = item.get("fileurl")
        if not url:
            return
        # We only download real files, not external URLs.
        if itype and itype != "file":
            return
        if url in seen_urls:
            return
        seen_urls.add(url)
        items.append(item)

    for item in module.get("contents") or []:
        add(item)

    if assign_meta:
        for item in assign_meta.get("introattachments") or []:
            add(item)
        for item in assign_meta.get("introfiles") or []:
            add(item)

    return items


async def _find_course(client: MoodleClient, course_id: int) -> Optional[dict[str, Any]]:
    for course in await client.list_courses():
        if course.get("id") == course_id:
            return course
    return None


async def download_course(
    client: MoodleClient,
    course_id: int,
    download_root: Path,
    moodle_url: str,
) -> DownloadManifest:
    course = await _find_course(client, course_id)
    if course is None:
        raise MoodleAPIError(
            f"Kurs {course_id} nicht in deinen eingeschriebenen Kursen gefunden."
        )

    category_id = course.get("category")
    category_name: Optional[str] = None
    if category_id is not None:
        try:
            category_name = await client.get_category_name(int(category_id))
        except (TypeError, ValueError):
            category_name = None

    course_dir = build_course_dir(download_root, moodle_url, category_name, course)
    course_dir.mkdir(parents=True, exist_ok=True)
    submissions_dir(course_dir).mkdir(parents=True, exist_ok=True)

    sections = await client.get_course_contents(course_id)
    assignments = await client.get_assignments(course_id)
    assign_by_cmid: dict[int, dict[str, Any]] = {}
    for a in assignments:
        cmid = a.get("cmid")
        if cmid is not None:
            try:
                assign_by_cmid[int(cmid)] = a
            except (TypeError, ValueError):
                continue

    manifest = DownloadManifest(
        course_id=course_id,
        course_name=course.get("fullname") or course.get("shortname") or f"Kurs {course_id}",
        course_dir=course_dir,
        markdown_path=course_dir / f"{sanitize_path_component(course.get('shortname') or course.get('fullname'))}.md",
    )

    attachments_by_cmid: dict[int, list[Path]] = {}

    for index, section in enumerate(sections):
        section_name = section.get("name") or f"Section {index}"
        section_dir = attachments_subdir(course_dir, section_name, index)
        modules = section.get("modules") or []

        for module in modules:
            if not module.get("visible", 1):
                continue
            cmid = module.get("id")
            if cmid is None:
                continue
            cmid = int(cmid)
            module_name = sanitize_path_component(module.get("name") or f"Modul {cmid}")
            module_dir = section_dir / module_name

            assign_meta = assign_by_cmid.get(cmid)
            files = _collect_attachments(module, assign_meta)
            if not files:
                continue

            module_dir.mkdir(parents=True, exist_ok=True)
            for item in files:
                filename = sanitize_path_component(item.get("filename") or "datei")
                dest = module_dir / filename

                expected_size = item.get("filesize")
                if (
                    dest.exists()
                    and isinstance(expected_size, int)
                    and expected_size > 0
                    and dest.stat().st_size == expected_size
                ):
                    manifest.skipped.append(dest)
                    attachments_by_cmid.setdefault(cmid, []).append(dest)
                    continue

                file_url = item.get("fileurl")
                if not file_url:
                    continue
                try:
                    written = await client.download_file(file_url, dest)
                except MoodleAPIError as err:
                    logger.warning("Download fehlgeschlagen für %s: %s", filename, err)
                    manifest.failed.append((str(dest), str(err)))
                    continue

                manifest.downloaded.append(dest)
                manifest.total_bytes += written
                attachments_by_cmid.setdefault(cmid, []).append(dest)

    md_text = render_course_markdown(
        course=course,
        category_name=category_name,
        sections=sections,
        assignments_by_cmid=assign_by_cmid,
        attachments_by_module=attachments_by_cmid,
        course_root=course_dir,
    )
    manifest.markdown_path.write_text(md_text, encoding="utf-8")

    # Drop a README hint into Abgaben/ on first creation so the user knows
    # what the folder is for.
    hint = submissions_dir(course_dir) / "README.md"
    if not hint.exists():
        hint.write_text(
            f"# {SUBMISSIONS_DIR}\n\n"
            "Lege hier Dateien ab, die du via `submit_assignment` einreichen möchtest.\n"
            "`submit_assignment` löst relative Pfade gegen diesen Ordner auf.\n",
            encoding="utf-8",
        )

    return manifest
