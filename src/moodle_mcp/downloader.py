"""Orchestrates ``download_course`` with the v2.1 per-module layout.

Walks every section and every visible module, and for each module creates
a dedicated folder with:

- ``<module>.md`` — the teacher-provided text
- ``Anhänge/`` — every downloadable file referenced by the module
- ``Abgabe/`` — only for ``modname == "assign"``; empty, for the user

Section and course level also get overview `.md` files that cross-link
into the tree, so the whole thing opens cleanly in Obsidian.

Re-running the tool is cheap: any file whose on-disk size matches the
Moodle record is skipped, and files/modules within a section are fetched
concurrently (bounded by ``max_concurrency``) to keep large courses fast.

Quiz, book, and forum modules get extra content beyond plain attachments:
quizzes get an auto-extracted ``Flashcards.md`` (see :mod:`moodle_mcp.quiz`),
books get their chapter text inlined, and forums get their discussion posts
inlined — all via the generic ``extra_sections`` hook on
:func:`moodle_mcp.markdown_renderer.render_module`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .html_utils import (
    extract_inline_base64_images,
    extract_pluginfile_filenames,
    html_to_plaintext,
)
from .markdown_renderer import (
    render_course_overview,
    render_module,
    render_section_overview,
)
from .moodle_client import MoodleAPIError, MoodleClient
from .paths import (
    ASSIGNMENTS_GROUP_DIR,
    build_attachment_dest_path,
    build_course_dir,
    build_module_dir,
    build_section_dir,
    classify_module_group,
    module_attachments_dir,
    module_submission_dir,
    sanitize_path_component,
)
from .quiz import extract_flashcards_from_review, render_flashcards_markdown


logger = logging.getLogger("moodle_mcp.downloader")

_CACHE_FILENAME = ".moodle-mcp-cache.json"
_DEFAULT_MAX_CONCURRENCY = 6

_QUIZ_MODNAMES = frozenset({"quiz"})
_BOOK_MODNAMES = frozenset({"book"})
_FORUM_MODNAMES = frozenset({"forum"})


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


# ---------------------------------------------------------------- per-course cache
def _load_course_cache(course_dir: Path) -> dict[str, Any]:
    """Load ``.moodle-mcp-cache.json`` (currently: processed quiz attempts).

    Missing/corrupt cache files are treated as empty rather than raising —
    the cache is a speed optimization, never a correctness requirement.
    """
    path = course_dir / _CACHE_FILENAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_course_cache(course_dir: Path, cache: dict[str, Any]) -> None:
    path = course_dir / _CACHE_FILENAME
    try:
        path.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
    except OSError as err:
        logger.warning("Konnte Cache-Datei nicht schreiben (%s): %s", path, err)


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


# ---------------------------------------------------------------- concurrent downloads
async def _download_items_concurrently(
    client: MoodleClient,
    items: list[dict[str, Any]],
    att_dir: Path,
    manifest: DownloadManifest,
    semaphore: asyncio.Semaphore,
) -> list[Path]:
    """Download every attachment item concurrently, bounded by ``semaphore``.

    ``asyncio.gather`` preserves the order of ``items`` in its result list
    regardless of completion order, so callers still get a stable path list.
    Failures are recorded in the manifest and reported as ``None`` (dropped
    from the result) rather than aborting the whole batch.
    """

    async def _one(item: dict[str, Any]) -> Optional[Path]:
        dest = build_attachment_dest_path(att_dir, item)
        expected_size = item.get("filesize")
        if (
            dest.exists()
            and isinstance(expected_size, int)
            and expected_size > 0
            and dest.stat().st_size == expected_size
        ):
            manifest.skipped.append(dest)
            return dest

        file_url = item.get("fileurl")
        if not file_url:
            return None

        filename = dest.name
        async with semaphore:
            try:
                written = await client.download_file(file_url, dest)
            except MoodleAPIError as err:
                logger.warning("Download fehlgeschlagen für %s: %s", filename, err)
                manifest.failed.append((str(dest), str(err)))
                return None

        manifest.downloaded.append(dest)
        manifest.total_bytes += written
        return dest

    results = await asyncio.gather(*[_one(item) for item in items])
    return [p for p in results if p is not None]


async def _download_module_files(
    client: MoodleClient,
    module: dict[str, Any],
    assign_meta: Optional[dict[str, Any]],
    module_dir: Path,
    manifest: DownloadManifest,
    semaphore: asyncio.Semaphore,
) -> list[Path]:
    """Download every file referenced by a module into ``module_dir/Anhänge/``.

    Returns the list of local file paths (both newly-downloaded and skipped),
    in item order. Failures are recorded in the manifest and do not abort
    the batch.
    """
    items = _collect_attachments(module, assign_meta)
    if not items:
        return []
    att_dir = module_attachments_dir(module_dir)
    return await _download_items_concurrently(client, items, att_dir, manifest, semaphore)


# ---------------------------------------------------------------- quiz / book / forum
async def _process_quiz_module(
    client: MoodleClient,
    module: dict[str, Any],
    quiz_meta: Optional[dict[str, Any]],
    module_dir: Path,
    quiz_cache: dict[str, Any],
) -> list[tuple[str, str]]:
    """Fetch the latest finished attempt and render ``Flashcards.md``.

    Only Moodle's own rendered review markup is used to determine the
    correct answer (see :mod:`moodle_mcp.quiz`) — question types without a
    recognizable ``.rightanswer`` block (e.g. essay) get a clear placeholder
    rather than a guessed answer.

    Skips the (relatively expensive) attempt-review fetch entirely if the
    latest finished attempt id hasn't changed since the last sync and
    ``Flashcards.md`` already exists — the caching layer requested for
    "nur geänderte Inhalte neu laden".
    """
    if quiz_meta is None or quiz_meta.get("id") is None:
        return []
    quiz_id = int(quiz_meta["id"])

    attempts = await client.get_quiz_user_attempts(quiz_id)
    finished = [a for a in attempts if a.get("state") == "finished"]
    if not finished:
        return [("Quiz", "Noch kein abgeschlossener Versuch — keine Flashcards verfügbar.")]

    latest = max(finished, key=lambda a: a.get("attempt") or a.get("id") or 0)
    attempt_id = latest.get("id")
    attempt_no = latest.get("attempt")
    if attempt_id is None:
        return []

    flashcards_path = module_dir / "Flashcards.md"
    cache_key = str(quiz_id)
    cached_entry = quiz_cache.get(cache_key)
    if (
        cached_entry
        and cached_entry.get("attempt_id") == attempt_id
        and flashcards_path.exists()
    ):
        logger.info(
            "Quiz %s: Versuch %s bereits verarbeitet, überspringe Review-Fetch",
            quiz_id, attempt_id,
        )
        cached_count = cached_entry.get("flashcard_count", "?")
        return [(
            "Quiz",
            f"{len(finished)} abgeschlossene(r) Versuch(e). "
            f"{cached_count} Flashcards (aus Cache, letzter Versuch {attempt_no}). "
            "Siehe `Flashcards.md`.",
        )]

    review = await client.get_quiz_attempt_review(int(attempt_id))
    questions = review.get("questions") or []
    flashcards = extract_flashcards_from_review(questions)
    quiz_name = module.get("name") or "Quiz"
    flashcards_path.write_text(
        render_flashcards_markdown(quiz_name, attempt_no, flashcards),
        encoding="utf-8",
    )
    quiz_cache[cache_key] = {"attempt_id": attempt_id, "flashcard_count": len(flashcards)}

    return [(
        "Quiz",
        f"{len(finished)} abgeschlossene(r) Versuch(e). "
        f"{len(flashcards)} Flashcards extrahiert aus Versuch {attempt_no}. "
        "Siehe `Flashcards.md`.",
    )]


async def _process_book_module(
    client: MoodleClient,
    book_meta: Optional[dict[str, Any]],
) -> list[tuple[str, str]]:
    """Inline every visible book chapter as its own ``extra_sections`` entry.

    ``core_course_get_contents`` doesn't expose book chapter text, so this
    is the only way to get full book coverage.
    """
    if book_meta is None or book_meta.get("id") is None:
        return []
    chapters = await client.get_book_chapters(int(book_meta["id"]))
    sections: list[tuple[str, str]] = []
    for chapter in chapters:
        if chapter.get("hidden"):
            continue
        title = chapter.get("title") or "Kapitel"
        text = html_to_plaintext(chapter.get("content") or "")
        if text:
            sections.append((title, text))
    return sections


async def _process_forum_module(
    client: MoodleClient,
    forum_meta: Optional[dict[str, Any]],
) -> list[tuple[str, str]]:
    """Inline every forum discussion (with all its posts) as ``extra_sections``."""
    if forum_meta is None or forum_meta.get("id") is None:
        return []
    discussions = await client.get_forum_discussions(int(forum_meta["id"]))
    sections: list[tuple[str, str]] = []
    for discussion in discussions:
        discussion_id = discussion.get("id")
        if discussion_id is None:
            discussion_id = discussion.get("discussion")
        name = discussion.get("name") or "Diskussion"
        posts = (
            await client.get_forum_discussion_posts(int(discussion_id))
            if discussion_id is not None
            else []
        )
        parts: list[str] = []
        for post in posts:
            author = (post.get("author") or {}).get("fullname") or post.get("userfullname") or "?"
            subject = post.get("subject") or ""
            message = html_to_plaintext(post.get("message") or "")
            header = f"**{author}**" + (f" — _{subject}_" if subject else "")
            block = f"{header}\n{message}".strip()
            if block:
                parts.append(block)
        body = "\n\n".join(parts)
        if body:
            sections.append((name, body))
    return sections


# ---------------------------------------------------------------- per-module pipeline
async def _process_module(
    client: MoodleClient,
    course: dict[str, Any],
    section: dict[str, Any],
    module: dict[str, Any],
    section_dir: Path,
    assign_by_cmid: dict[int, dict[str, Any]],
    assign_by_instance: dict[int, dict[str, Any]],
    quiz_by_instance: dict[int, dict[str, Any]],
    book_by_instance: dict[int, dict[str, Any]],
    forum_by_instance: dict[int, dict[str, Any]],
    quiz_cache: dict[str, Any],
    manifest: DownloadManifest,
    semaphore: asyncio.Semaphore,
) -> Optional[tuple[dict[str, Any], Optional[dict[str, Any]], Path]]:
    """Download + render one module. Returns ``None`` for hidden/malformed modules."""
    if not module.get("visible", 1):
        return None
    cmid = module.get("id")
    if cmid is None:
        return None
    try:
        cmid_int = int(cmid)
    except (TypeError, ValueError):
        return None

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
        semaphore=semaphore,
    )
    inline_paths, inline_image_map = _save_inline_images(
        module=module,
        assign_meta=assign_meta,
        module_dir=module_dir,
        manifest=manifest,
    )
    attachment_paths = attachment_paths + inline_paths

    # Abgabe/ folder only for assign-type modules
    if classify_module_group(modname) == ASSIGNMENTS_GROUP_DIR:
        module_submission_dir(module_dir).mkdir(parents=True, exist_ok=True)

    instance_int: Optional[int] = None
    instance = module.get("instance")
    if instance is not None:
        try:
            instance_int = int(instance)
        except (TypeError, ValueError):
            instance_int = None

    extra_sections: list[tuple[str, str]] = []
    if modname in _QUIZ_MODNAMES:
        quiz_meta = quiz_by_instance.get(instance_int) if instance_int is not None else None
        extra_sections = await _process_quiz_module(
            client, module, quiz_meta, module_dir, quiz_cache
        )
    elif modname in _BOOK_MODNAMES:
        book_meta = book_by_instance.get(instance_int) if instance_int is not None else None
        extra_sections = await _process_book_module(client, book_meta)
    elif modname in _FORUM_MODNAMES:
        forum_meta = forum_by_instance.get(instance_int) if instance_int is not None else None
        extra_sections = await _process_forum_module(client, forum_meta)

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
            extra_sections=extra_sections,
        ),
        encoding="utf-8",
    )

    return module, assign_meta, module_md_path


async def download_course(
    client: MoodleClient,
    course_id: int,
    download_root: Path,
    moodle_url: str,
    max_concurrency: int = _DEFAULT_MAX_CONCURRENCY,
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

    # Quiz/book/forum metadata is fetched once per course (like assignments
    # above) rather than once per module — these WS calls are cheap relative
    # to the per-attempt/per-chapter/per-discussion follow-ups below.
    quiz_by_instance: dict[int, dict[str, Any]] = {}
    for z in await client.get_quizzes_by_courses([course_id]):
        if z.get("id") is not None:
            try:
                quiz_by_instance[int(z["id"])] = z
            except (TypeError, ValueError):
                pass

    book_by_instance: dict[int, dict[str, Any]] = {}
    for b in await client.get_books_by_courses([course_id]):
        if b.get("id") is not None:
            try:
                book_by_instance[int(b["id"])] = b
            except (TypeError, ValueError):
                pass

    forum_by_instance: dict[int, dict[str, Any]] = {}
    for f in await client.get_forums_by_courses([course_id]):
        if f.get("id") is not None:
            try:
                forum_by_instance[int(f["id"])] = f
            except (TypeError, ValueError):
                pass

    quiz_cache: dict[str, Any] = _load_course_cache(course_dir).get("quiz_attempts", {})
    semaphore = asyncio.Semaphore(max(1, max_concurrency))

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

        modules = section.get("modules") or []

        # Modules within a section are processed concurrently (files +
        # quiz/book/forum fetches), bounded by ``semaphore``.
        # asyncio.gather preserves input order in its results, so
        # Section.md still lists modules in their original Moodle order.
        results = await asyncio.gather(*[
            _process_module(
                client=client,
                course=course,
                section=section,
                module=module,
                section_dir=section_dir,
                assign_by_cmid=assign_by_cmid,
                assign_by_instance=assign_by_instance,
                quiz_by_instance=quiz_by_instance,
                book_by_instance=book_by_instance,
                forum_by_instance=forum_by_instance,
                quiz_cache=quiz_cache,
                manifest=manifest,
                semaphore=semaphore,
            )
            for module in modules
        ])
        modules_with_paths = [r for r in results if r is not None]
        manifest.module_count += len(modules_with_paths)

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

    _save_course_cache(course_dir, {"quiz_attempts": quiz_cache})

    return manifest
