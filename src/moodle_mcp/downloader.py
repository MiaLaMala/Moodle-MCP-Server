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

from .html_utils import extract_inline_base64_images, extract_pluginfile_filenames
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


def _module_html_bodies(
    module: dict[str, Any],
    assign_meta: Optional[dict[str, Any]],
) -> list[str]:
    """Return every HTML body that may carry ``@@PLUGINFILE@@`` tokens.

    Covers the assignment intro, generic module description, and the page
    content stored inline in ``module["contents"]`` (Moodle ``mod_page``).
    Empty strings are dropped so callers can iterate without ``if`` checks.
    """
    bodies: list[str] = []
    if assign_meta and assign_meta.get("intro"):
        bodies.append(str(assign_meta["intro"]))
    description = module.get("description")
    if description:
        bodies.append(str(description))
    for item in module.get("contents") or []:
        if isinstance(item, dict) and item.get("type") == "content":
            content = item.get("content")
            if content:
                bodies.append(str(content))
    return bodies


def _collect_attachments(
    module: dict[str, Any],
    assign_meta: Optional[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collect every downloadable file referenced by a module.

    Looks at three sources, in order of decreasing reliability:

    1. The module's ``contents`` array (file-type entries: ``mod_resource``,
       ``mod_page`` inline files, ``mod_folder``…).
    2. For assignments, both ``introattachments`` (separate attachment list)
       AND ``introfiles`` (files in the ``mod_assign/intro`` filearea that
       are referenced inline via ``@@PLUGINFILE@@`` tokens in the intro
       HTML). Items in either list are added regardless of whether the
       parsed HTML references them — Moodle sometimes serves attachments
       without an explicit intro link.
    3. As a fallback the relevant HTML bodies are scanned for
       ``@@PLUGINFILE@@`` tokens, and any referenced filename that we have
       a known fileurl for (from steps 1–2) is included even if the source
       list didn't carry a ``type`` field. Unresolved references are
       logged at debug level — they typically point to files in a filearea
       we don't have URLs for.
    """
    items: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    by_basename: dict[str, dict[str, Any]] = {}

    def add(item: Any) -> None:
        if not isinstance(item, dict):
            return
        url = item.get("fileurl")
        if not url:
            return
        itype = item.get("type")
        # The two assign filearea endpoints don't always set ``type``; treat
        # missing or "file" as a downloadable file, but drop "content",
        # "url", etc. that may live alongside files in ``module["contents"]``.
        if itype and itype != "file":
            return
        if url in seen_urls:
            return
        seen_urls.add(url)
        items.append(item)
        filename = item.get("filename")
        if filename:
            by_basename.setdefault(str(filename), item)

    for item in module.get("contents") or []:
        add(item)
    if assign_meta:
        for item in assign_meta.get("introattachments") or []:
            add(item)
        for item in assign_meta.get("introfiles") or []:
            add(item)

    # Diagnostic pass: warn about unresolved @@PLUGINFILE@@ refs so the
    # gap is visible in logs rather than producing a silently broken .md.
    referenced: list[str] = []
    for body in _module_html_bodies(module, assign_meta):
        referenced.extend(extract_pluginfile_filenames(body))
    if referenced:
        unresolved = [name for name in referenced if name not in by_basename]
        if unresolved:
            logger.warning(
                "Modul %r referenziert @@PLUGINFILE@@-Dateien, für die kein "
                "fileurl bekannt ist: %s",
                module.get("name") or module.get("id"),
                ", ".join(sorted(set(unresolved))),
            )

    return items


def _save_inline_images(
    module: dict[str, Any],
    assign_meta: Optional[dict[str, Any]],
    module_dir: Path,
    manifest: DownloadManifest,
) -> tuple[list[Path], dict[str, Path]]:
    """Decode every ``<img src="data:image/...;base64,...">`` to disk.

    Hamburg's Moodle stores entire assignment lesson texts as base64
    PNGs embedded directly in the intro HTML — there are no
    ``introfiles`` / ``introattachments`` entries for them. This routine
    pulls them out so they exist as real files inside ``Anhänge/`` and
    returns a digest → path mapping that the renderer uses to rewrite
    the ``<img src="data:...">`` tags into relative ``![](...)`` links.

    Identical images (same SHA-256 digest) are written once.
    """
    bodies = _module_html_bodies(module, assign_meta)
    if not bodies:
        return [], {}

    saved: list[Path] = []
    digest_to_path: dict[str, Path] = {}

    for body in bodies:
        for image in extract_inline_base64_images(body):
            if image.digest in digest_to_path:
                continue
            att_dir = module_attachments_dir(module_dir)
            att_dir.mkdir(parents=True, exist_ok=True)
            dest = att_dir / image.filename

            # Incremental sync: skip if already on disk with matching size.
            if dest.exists() and dest.stat().st_size == len(image.data):
                manifest.skipped.append(dest)
            else:
                try:
                    dest.write_bytes(image.data)
                except OSError as err:
                    logger.warning(
                        "Konnte Inline-Bild nicht schreiben (%s): %s", dest, err
                    )
                    manifest.failed.append((str(dest), str(err)))
                    continue
                manifest.downloaded.append(dest)
                manifest.total_bytes += len(image.data)

            digest_to_path[image.digest] = dest
            saved.append(dest)

    return saved, digest_to_path


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
    assign_by_instance: dict[int, dict[str, Any]] = {}
    for a in assignments:
        cmid = a.get("cmid")
        if cmid is not None:
            try:
                assign_by_cmid[int(cmid)] = a
            except (TypeError, ValueError):
                pass
        instance_id = a.get("id")
        if instance_id is not None:
            try:
                assign_by_instance[int(instance_id)] = a
            except (TypeError, ValueError):
                pass

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
            if assign_meta is None and modname == "assign":
                instance = module.get("instance")
                if instance is not None:
                    try:
                        assign_meta = assign_by_instance.get(int(instance))
                    except (TypeError, ValueError):
                        assign_meta = None
            attachment_paths = await _download_module_files(
                client=client,
                module=module,
                assign_meta=assign_meta,
                module_dir=module_dir,
                manifest=manifest,
            )
            inline_paths, inline_image_map = _save_inline_images(
                module=module,
                assign_meta=assign_meta,
                module_dir=module_dir,
                manifest=manifest,
            )
            attachment_paths.extend(inline_paths)

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
                    inline_image_map=inline_image_map,
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
