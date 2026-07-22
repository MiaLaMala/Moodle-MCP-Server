"""Push a locally-synced Moodle course into a self-hosted Open Notebook instance.

Lernfeld-mapping: the Moodle *category* (e.g. "Fachinformatik") becomes the
Open Notebook *notebook*; the course itself becomes a tag on every pushed
source (via Open Notebook's ``topics`` field — there is no separate "tags"
concept in its API), so multiple courses inside one Lernfeld stay
distinguishable inside a single notebook.

Runs ``download_course`` first to make sure the local vault is current,
then walks every module ``.md`` (skipping the ``Kurs.md``/``Section.md``
overview files, which only contain cross-links, not unique content) and
pushes it as a ``type="text"`` Source. Quiz ``Flashcards.md`` files are
pushed as their own, separately-tagged source so they're easy to find for
exam prep inside Open Notebook.

Idempotency: a ``.moodle-mcp-open-notebook.json`` cache (keyed by the file's
path relative to the course dir) tracks the content hash + Open Notebook
source id of the last push. Unchanged files are skipped; changed files are
re-pushed by deleting the stale source (Open Notebook's ``SourceUpdate``
can only change title/topics, not re-process new content) and creating a
fresh one. A single failed push is logged and counted, not fatal to the run.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from .config import MoodleConfig
from .downloader import download_course as run_download_course
from .moodle_client import MoodleClient
from .open_notebook_client import OpenNotebookClient, OpenNotebookError


logger = logging.getLogger("moodle_mcp.open_notebook_sync")

_SYNC_CACHE_FILENAME = ".moodle-mcp-open-notebook.json"
_SKIP_FILENAMES = {"Kurs.md", "Section.md"}


def _load_sync_cache(course_dir: Path) -> dict[str, Any]:
    path = course_dir / _SYNC_CACHE_FILENAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_sync_cache(course_dir: Path, cache: dict[str, Any]) -> None:
    path = course_dir / _SYNC_CACHE_FILENAME
    try:
        path.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
    except OSError as err:
        logger.warning(
            "Konnte Open-Notebook-Sync-Cache nicht schreiben (%s): %s", path, err
        )


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


async def push_course_to_open_notebook(
    moodle_client: MoodleClient,
    config: MoodleConfig,
    course_id: int,
) -> str:
    """Sync + push a course's Markdown content into Open Notebook.

    Returns a human-readable summary string (mirrors the style of the
    other tools in ``server.py``).
    """
    if not config.has_open_notebook:
        return (
            "Fehler: MOODLE_OPEN_NOTEBOOK_URL ist nicht gesetzt — "
            "push_to_open_notebook ist deaktiviert. Siehe .env.example."
        )

    manifest = await run_download_course(
        client=moodle_client,
        course_id=course_id,
        download_root=config.download_root,
        moodle_url=config.url or "",
        max_concurrency=config.max_concurrency,
    )
    course_dir = manifest.course_dir

    # Lernfeld = Moodle category = the folder directly above the course dir
    # (see paths.build_course_dir: <root>/<host>/<category>/<course>/).
    category_name = course_dir.parent.name
    course_tag = course_dir.name

    pushed = skipped = failed = 0

    async with OpenNotebookClient(
        config.open_notebook_url or "", config.open_notebook_password
    ) as on_client:
        try:
            notebook = await on_client.get_or_create_notebook(
                category_name,
                description=(
                    f"Moodle-Lernfeld: {category_name} "
                    "(automatisch von moodle-mcp gepflegt)"
                ),
            )
        except OpenNotebookError as err:
            return f"Fehler: konnte Open-Notebook-Notebook nicht anlegen/finden: {err}"

        notebook_id = notebook.get("id")
        if not notebook_id:
            return f"Fehler: Notebook-Antwort ohne id: {notebook}"

        cache = _load_sync_cache(course_dir)

        md_files = sorted(
            p for p in course_dir.rglob("*.md") if p.name not in _SKIP_FILENAMES
        )
        for md_path in md_files:
            rel = str(md_path.relative_to(course_dir))
            try:
                text = md_path.read_text(encoding="utf-8")
            except OSError as err:
                logger.warning("Konnte %s nicht lesen: %s", md_path, err)
                failed += 1
                continue
            if not text.strip():
                continue

            digest = _content_hash(text)
            cached_entry = cache.get(rel)
            if cached_entry and cached_entry.get("hash") == digest:
                skipped += 1
                continue

            is_flashcards = md_path.name == "Flashcards.md"
            title = f"{course_tag} — {md_path.parent.name}" + (
                " (Flashcards)" if is_flashcards else ""
            )
            topics = [course_tag, "flashcards" if is_flashcards else "material"]

            if cached_entry and cached_entry.get("source_id"):
                try:
                    await on_client.delete_source(cached_entry["source_id"])
                except OpenNotebookError as err:
                    logger.info(
                        "Alte Source %s konnte nicht gelöscht werden (evtl. schon "
                        "weg): %s",
                        cached_entry["source_id"], err,
                    )

            try:
                source = await on_client.create_text_source(
                    notebook_id=notebook_id,
                    title=title,
                    content=text,
                    topics=topics,
                )
            except OpenNotebookError as err:
                logger.warning("Push fehlgeschlagen für %s: %s", rel, err)
                failed += 1
                continue

            cache[rel] = {"source_id": source.get("id"), "hash": digest}
            pushed += 1

        _save_sync_cache(course_dir, cache)

    lines = [
        f"# Push zu Open Notebook — {manifest.course_name}",
        "",
        f"- Notebook (Lernfeld): **{category_name}**",
        f"- Tag: **{course_tag}**",
        f"- Neu/aktualisiert gepusht: **{pushed}**",
        f"- Unverändert übersprungen: **{skipped}**",
    ]
    if failed:
        lines.append(f"- Fehlgeschlagen: **{failed}** (siehe Server-Log)")
    return "\n".join(lines)
