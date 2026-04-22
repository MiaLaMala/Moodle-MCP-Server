"""Orchestrates ``download_course`` with the v2.1 per-module layout.

Walks every section and every visible module, and for each module creates
a dedicated folder with:

- ``<module>.md`` — the teacher-provided text
- ``Anhänge/`` — every downloadable file referenced by the module
- ``Abgabe/`` — only for ``modname == "assign"``; empty, for the user

Section and course level also get overview `.md` files that cross-link
into the tree, so the whole thing opens cleanly in Obsidian.

Re-running the tool is cheap: any file whose on-disk size matches the
Moodle record is skipped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .markdown_renderer import (
    render_course_overview,
    render_module,
    render_section_overview,
)
from .moodle_client import MoodleAPIError, MoodleClient
from .paths import (
    ASSIGNMENTS_GROUP_DIR,
    build_course_dir,
    build_module_dir,
    build_section_dir,
    classify_module_group,
    module_attachments_dir,
    module_submission_dir,
    sanitize_path_component,
)


logger = logging.getLogger("moodle_mcp.downloader")


@dataclass
class DownloadManifest:
    course_id: int
    course_name: str
    course_dir: Path
    kurs_md_path: Path
    downloaded: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    module_count: int = 0
    section_count: int = 0
    total_bytes: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "course_id": self.course_id,
            "course_name": self.course_name,
            "course_dir": str(self.course_dir),
            "kurs_md_path": str(self.kurs_md_path),
            "downloaded_count": len(self.downloaded),
            "skipped_count": len(self.skipped),
            "failed_count": len(self.failed),
            "module_count": self.module_count,
            "section_count": self.section_count,
            "total_bytes": self.total_bytes,
            "failed": [{"file": name, "error": err} for name, err in self.failed],
        }


def _collect_attachments(
    module: dict[str, Any],
    assign_meta: Optional[dict[str, Any]],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    def add(item: Any) -> None:
        if not isinstance(item, dict):
            return
        itype = item.get("type")
        url = item.get("fileurl")
        if not url:
            return
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


async def _download_module_files(
    client: MoodleClient,
    module: dict[str, Any],
    assign_meta: Optional[dict[str, Any]],
    module_dir: Path,
    manifest: DownloadManifest,
) -> list[Path]:
    """Download every file referenced by a module into ``module_dir/Anhänge/``.

    Returns the list of local file paths (both newly-downloaded and skipped),
    in the order they were encountered. Failures are recorded in the manifest
    and do not abort the loop.
    """
    items = _collect_attachments(module, assign_meta)
    if not items:
        return []

    att_dir = module_attachments_dir(module_dir)
    att_dir.mkdir(parents=True, exist_ok=True)

    result: list[Path] = []
    for item in items:
        filename = sanitize_path_component(item.get("filename") or "datei")
        dest = att_dir / filename

        expected_size = item.get("filesize")
        if (
            dest.exists()
            and isinstance(expected_size, int)
            and expected_size > 0
            and dest.stat().st_size == expected_size
        ):
            manifest.skipped.append(dest)
            result.append(dest)
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
        result.append(dest)

    return result


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

    kurs_md_path = course_dir / "Kurs.md"
    manifest = DownloadManifest(
        course_id=course_id,
        course_name=course.get("fullname") or course.get("shortname") or f"Kurs {course_id}",
        course_dir=course_dir,
        kurs_md_path=kurs_md_path,
    )

    sections_with_paths: list[tuple[dict[str, Any], Path]] = []

    for idx, section in enumerate(sections):
        section_name = section.get("name") or f"Section {idx}"
        section_dir = build_section_dir(course_dir, section_name, idx)
        section_dir.mkdir(parents=True, exist_ok=True)
        section_md_path = section_dir / "Section.md"

        modules_with_paths: list[tuple[dict[str, Any], dict[str, Any] | None, Path]] = []
        modules = section.get("modules") or []

        for module in modules:
            if not module.get("visible", 1):
                continue
            cmid = module.get("id")
            if cmid is None:
                continue
            try:
                cmid_int = int(cmid)
            except (TypeError, ValueError):
                continue

            module_name = module.get("name") or f"Modul {cmid_int}"
            modname = module.get("modname") or "unknown"
            module_dir = build_module_dir(section_dir, module_name, modname)
            module_dir.mkdir(parents=True, exist_ok=True)

            assign_meta = assign_by_cmid.get(cmid_int)
            attachment_paths = await _download_module_files(
                client=client,
                module=module,
                assign_meta=assign_meta,
                module_dir=module_dir,
                manifest=manifest,
            )

            # Abgabe/ folder only for assign-type modules
            if classify_module_group(modname) == ASSIGNMENTS_GROUP_DIR:
                module_submission_dir(module_dir).mkdir(parents=True, exist_ok=True)

            module_md_name = sanitize_path_component(module_name) + ".md"
            module_md_path = module_dir / module_md_name
            module_md_path.write_text(
                render_module(
                    course=course,
                    section=section,
                    module=module,
                    assign_meta=assign_meta,
                    module_md_path=module_md_path,
                    attachment_paths=attachment_paths,
                ),
                encoding="utf-8",
            )

            modules_with_paths.append((module, assign_meta, module_md_path))
            manifest.module_count += 1

        section_md_path.write_text(
            render_section_overview(
                course=course,
                section=section,
                section_index=idx,
                modules_with_paths=modules_with_paths,
                section_md_path=section_md_path,
            ),
            encoding="utf-8",
        )
        sections_with_paths.append((section, section_md_path))
        manifest.section_count += 1

    kurs_md_path.write_text(
        render_course_overview(
            course=course,
            category_name=category_name,
            sections_with_paths=sections_with_paths,
            kurs_md_path=kurs_md_path,
        ),
        encoding="utf-8",
    )

    return manifest
