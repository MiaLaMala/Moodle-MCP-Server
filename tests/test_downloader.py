"""Tests for :mod:`moodle_mcp.downloader` focused on intro-file handling."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import base64
import hashlib

from moodle_mcp.downloader import (
    DownloadManifest,
    _collect_attachments,
    _save_inline_images,
    download_course,
)


_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae"
    "426082"
)
_PNG_B64 = base64.b64encode(_PNG_BYTES).decode("ascii")
_PNG_DIGEST = hashlib.sha256(_PNG_BYTES).hexdigest()[:12]


# ---------------------------------------------------------------- _collect_attachments
def _file_item(name: str, url: str | None = None, itype: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"filename": name, "filesize": len(name)}
    if url is not None:
        item["fileurl"] = url
    if itype is not None:
        item["type"] = itype
    return item


def test_collects_assignment_introfiles_without_type_field() -> None:
    """``introfiles`` items from ``mod_assign_get_assignments`` don't always
    carry a ``type`` field — they must still be downloaded."""
    module = {"id": 1, "modname": "assign", "name": "A"}
    assign_meta = {
        "id": 555,
        "intro": '<p><a href="@@PLUGINFILE@@/Matrix.pdf">M</a></p>',
        "introattachments": [],
        "introfiles": [_file_item("Matrix.pdf", url="https://m/pluginfile.php/1/mod_assign/intro/Matrix.pdf")],
    }
    items = _collect_attachments(module, assign_meta)
    assert len(items) == 1
    assert items[0]["filename"] == "Matrix.pdf"


def test_collects_both_introattachments_and_introfiles() -> None:
    module = {"id": 1, "modname": "assign", "name": "A"}
    assign_meta = {
        "introattachments": [_file_item("attached.pdf", url="https://m/a")],
        "introfiles": [_file_item("inline.pdf", url="https://m/b")],
    }
    names = [i["filename"] for i in _collect_attachments(module, assign_meta)]
    assert names == ["attached.pdf", "inline.pdf"]


def test_skips_duplicates_by_url() -> None:
    module = {"id": 1, "modname": "assign", "name": "A"}
    assign_meta = {
        "introattachments": [_file_item("x.pdf", url="https://m/x")],
        "introfiles": [_file_item("x.pdf", url="https://m/x")],
    }
    items = _collect_attachments(module, assign_meta)
    assert len(items) == 1


def test_drops_non_file_content_entries() -> None:
    module = {
        "id": 1,
        "modname": "page",
        "name": "P",
        "contents": [
            {"type": "content", "content": "<p>body</p>"},
            _file_item("doc.pdf", url="https://m/doc", itype="file"),
        ],
    }
    items = _collect_attachments(module, assign_meta=None)
    assert [i["filename"] for i in items] == ["doc.pdf"]


def test_unresolved_pluginfile_ref_is_warned(caplog: pytest.LogCaptureFixture) -> None:
    """If the intro HTML references a filename we have no fileurl for, we
    must log a warning so the regression is visible — instead of silently
    producing a broken .md."""
    module = {
        "id": 1,
        "modname": "assign",
        "name": "Programmiersprachen, Werkzeuge",
    }
    assign_meta = {
        "intro": '<p><a href="@@PLUGINFILE@@/Werkzeuge.pdf">WZ</a></p>',
        "introattachments": [],
        "introfiles": [],
    }
    with caplog.at_level("WARNING", logger="moodle_mcp.downloader"):
        _collect_attachments(module, assign_meta)
    assert any("Werkzeuge.pdf" in rec.message for rec in caplog.records)


def test_no_warning_when_pluginfile_ref_is_satisfied(caplog: pytest.LogCaptureFixture) -> None:
    module = {"id": 1, "modname": "assign", "name": "A"}
    assign_meta = {
        "intro": '<p><a href="@@PLUGINFILE@@/Matrix.pdf">x</a></p>',
        "introfiles": [_file_item("Matrix.pdf", url="https://m/Matrix.pdf")],
    }
    with caplog.at_level("WARNING", logger="moodle_mcp.downloader"):
        _collect_attachments(module, assign_meta)
    assert not any("PLUGINFILE" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------- end-to-end
class _StubClient:
    """Minimal MoodleClient stand-in driving ``download_course``."""

    def __init__(
        self,
        sections: list[dict[str, Any]],
        assignments: list[dict[str, Any]],
        course: dict[str, Any],
        quizzes: list[dict[str, Any]] | None = None,
        books: list[dict[str, Any]] | None = None,
        forums: list[dict[str, Any]] | None = None,
        quiz_attempts: dict[int, list[dict[str, Any]]] | None = None,
        quiz_reviews: dict[int, dict[str, Any]] | None = None,
        book_chapters: dict[int, list[dict[str, Any]]] | None = None,
        forum_discussions: dict[int, list[dict[str, Any]]] | None = None,
        forum_posts: dict[int, list[dict[str, Any]]] | None = None,
    ) -> None:
        self._sections = sections
        self._assignments = assignments
        self._course = course
        self._quizzes = quizzes or []
        self._books = books or []
        self._forums = forums or []
        self._quiz_attempts = quiz_attempts or {}
        self._quiz_reviews = quiz_reviews or {}
        self._book_chapters = book_chapters or {}
        self._forum_discussions = forum_discussions or {}
        self._forum_posts = forum_posts or {}
        self.downloads: list[str] = []
        self.review_fetch_count = 0

    async def list_courses(self) -> list[dict[str, Any]]:
        return [self._course]

    async def get_category_name(self, category_id: int) -> str | None:
        return "TestKategorie"

    async def get_course_contents(self, course_id: int) -> list[dict[str, Any]]:
        return self._sections

    async def get_assignments(self, course_id: int) -> list[dict[str, Any]]:
        return self._assignments

    async def get_quizzes_by_courses(self, course_ids: list[int]) -> list[dict[str, Any]]:
        return self._quizzes

    async def get_quiz_user_attempts(self, quiz_id: int) -> list[dict[str, Any]]:
        return self._quiz_attempts.get(quiz_id, [])

    async def get_quiz_attempt_review(self, attempt_id: int) -> dict[str, Any]:
        self.review_fetch_count += 1
        return self._quiz_reviews.get(attempt_id, {})

    async def get_books_by_courses(self, course_ids: list[int]) -> list[dict[str, Any]]:
        return self._books

    async def get_book_chapters(self, book_id: int) -> list[dict[str, Any]]:
        return self._book_chapters.get(book_id, [])

    async def get_forums_by_courses(self, course_ids: list[int]) -> list[dict[str, Any]]:
        return self._forums

    async def get_forum_discussions(self, forum_id: int) -> list[dict[str, Any]]:
        return self._forum_discussions.get(forum_id, [])

    async def get_forum_discussion_posts(self, discussion_id: int) -> list[dict[str, Any]]:
        return self._forum_posts.get(discussion_id, [])

    async def download_file(self, file_url: str, dest_path: Path) -> int:
        self.downloads.append(file_url)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(b"PDF")
        return 3


@pytest.mark.asyncio
async def test_download_course_finds_assignment_by_instance_when_cmid_missing(
    tmp_path: Path,
) -> None:
    """Regression: in the field, ``mod_assign_get_assignments`` sometimes
    returns rows without a ``cmid``. Fall back to matching by instance id
    so introfiles still get downloaded."""
    course = {"id": 40794, "fullname": "Testkurs", "category": 1, "shortname": "TC"}
    sections = [{
        "name": "Umfeld der Softwareentwicklung",
        "modules": [{
            "id": 9001,
            "instance": 402430,
            "modname": "assign",
            "name": "Programmiersprachen, Werkzeuge",
            "visible": 1,
            "contents": [],
        }],
    }]
    assignments = [{
        # NB: no "cmid" — matches the broken Moodle responses we want to handle
        "id": 402430,
        "intro": '<p><a href="@@PLUGINFILE@@/Werkzeuge.pdf">WZ</a></p>',
        "introattachments": [],
        "introfiles": [{
            "filename": "Werkzeuge.pdf",
            "filesize": 3,
            "fileurl": "https://moodle/pluginfile.php/1/mod_assign/intro/Werkzeuge.pdf",
        }],
    }]
    client = _StubClient(sections, assignments, course)

    manifest = await download_course(client, 40794, tmp_path, "https://moodle.example")

    assert client.downloads == [
        "https://moodle/pluginfile.php/1/mod_assign/intro/Werkzeuge.pdf"
    ]
    # Each downloaded file lives inside the module's Anhänge/ folder.
    written = manifest.downloaded
    assert len(written) == 1
    assert written[0].parent.name == "Anhänge"
    assert written[0].name == "Werkzeuge.pdf"


# ---------------------------------------------------------------- quiz / book / forum / folder
def _quiz_question_html(state: str, qtext: str, rightanswer: str, ablock: str) -> str:
    return (
        f'<div class="que multichoice {state}">'
        f'<div class="qtext">{qtext}</div>'
        f'<div class="ablock">{ablock}</div>'
        f'<div class="rightanswer">{rightanswer}</div>'
        "</div>"
    )


@pytest.mark.asyncio
async def test_download_course_quiz_module_writes_flashcards(tmp_path: Path) -> None:
    course = {"id": 1, "fullname": "Testkurs", "category": 1, "shortname": "TC"}
    sections = [{
        "name": "S1",
        "modules": [{
            "id": 1,
            "instance": 500,
            "modname": "quiz",
            "name": "Abschlusstest",
            "visible": 1,
            "contents": [],
        }],
    }]
    client = _StubClient(
        sections, [], course,
        quizzes=[{"id": 500, "name": "Abschlusstest"}],
        quiz_attempts={500: [{"id": 9, "attempt": 1, "state": "finished"}]},
        quiz_reviews={9: {"questions": [
            {"slot": 1, "html": _quiz_question_html("correct", "Q1?", "A1", "A1")},
        ]}},
    )

    manifest = await download_course(client, 1, tmp_path, "https://moodle.example")

    module_dir = manifest.course_dir / "Kurse" / "S1" / "Infotexte" / "Abschlusstest"
    flashcards_path = module_dir / "Flashcards.md"
    assert flashcards_path.exists()
    text = flashcards_path.read_text(encoding="utf-8")
    assert "Q1?" in text
    assert "A1" in text
    assert client.review_fetch_count == 1

    module_md = (module_dir / "Abschlusstest.md").read_text(encoding="utf-8")
    assert "## Quiz" in module_md
    assert "Flashcards.md" in module_md


@pytest.mark.asyncio
async def test_download_course_quiz_module_skips_review_refetch_when_unchanged(
    tmp_path: Path,
) -> None:
    """Re-running with the same latest attempt id must not re-fetch the review."""
    course = {"id": 1, "fullname": "Testkurs", "category": 1, "shortname": "TC"}
    sections = [{
        "name": "S1",
        "modules": [{
            "id": 1, "instance": 500, "modname": "quiz",
            "name": "Abschlusstest", "visible": 1, "contents": [],
        }],
    }]
    client = _StubClient(
        sections, [], course,
        quizzes=[{"id": 500, "name": "Abschlusstest"}],
        quiz_attempts={500: [{"id": 9, "attempt": 1, "state": "finished"}]},
        quiz_reviews={9: {"questions": [
            {"slot": 1, "html": _quiz_question_html("correct", "Q1?", "A1", "A1")},
        ]}},
    )

    await download_course(client, 1, tmp_path, "https://moodle.example")
    assert client.review_fetch_count == 1

    await download_course(client, 1, tmp_path, "https://moodle.example")
    assert client.review_fetch_count == 1  # cache hit, no second fetch


@pytest.mark.asyncio
async def test_download_course_book_module_inlines_chapters(tmp_path: Path) -> None:
    course = {"id": 1, "fullname": "Testkurs", "category": 1, "shortname": "TC"}
    sections = [{
        "name": "S1",
        "modules": [{
            "id": 1, "instance": 600, "modname": "book",
            "name": "Lehrbuch", "visible": 1, "contents": [],
        }],
    }]
    client = _StubClient(
        sections, [], course,
        books=[{"id": 600, "name": "Lehrbuch"}],
        book_chapters={600: [
            {"title": "Kapitel 1", "content": "<p>Inhalt von Kapitel 1</p>", "hidden": 0},
            {"title": "Verstecktes Kapitel", "content": "<p>geheim</p>", "hidden": 1},
        ]},
    )

    manifest = await download_course(client, 1, tmp_path, "https://moodle.example")

    module_md = (
        manifest.course_dir / "Kurse" / "S1" / "Infotexte" / "Lehrbuch" / "Lehrbuch.md"
    ).read_text(encoding="utf-8")
    assert "## Kapitel 1" in module_md
    assert "Inhalt von Kapitel 1" in module_md
    assert "geheim" not in module_md  # hidden chapters are skipped


@pytest.mark.asyncio
async def test_download_course_forum_module_inlines_discussions_and_posts(
    tmp_path: Path,
) -> None:
    course = {"id": 1, "fullname": "Testkurs", "category": 1, "shortname": "TC"}
    sections = [{
        "name": "S1",
        "modules": [{
            "id": 1, "instance": 700, "modname": "forum",
            "name": "Ankündigungen", "visible": 1, "contents": [],
        }],
    }]
    client = _StubClient(
        sections, [], course,
        forums=[{"id": 700, "name": "Ankündigungen"}],
        forum_discussions={700: [{"id": 1, "name": "Diskussion A"}]},
        forum_posts={1: [
            {
                "author": {"fullname": "Max Mustermann"},
                "subject": "Betreff X",
                "message": "<p>Hallo Welt</p>",
            }
        ]},
    )

    manifest = await download_course(client, 1, tmp_path, "https://moodle.example")

    module_md = (
        manifest.course_dir / "Kurse" / "S1" / "Infotexte" / "Ankündigungen" / "Ankündigungen.md"
    ).read_text(encoding="utf-8")
    assert "## Diskussion A" in module_md
    assert "Max Mustermann" in module_md
    assert "Betreff X" in module_md
    assert "Hallo Welt" in module_md


@pytest.mark.asyncio
async def test_download_course_folder_module_preserves_subfolder_structure(
    tmp_path: Path,
) -> None:
    """Two same-named files in different mod_folder subfolders must not collide."""
    course = {"id": 1, "fullname": "Testkurs", "category": 1, "shortname": "TC"}
    sections = [{
        "name": "S1",
        "modules": [{
            "id": 1, "instance": 800, "modname": "folder",
            "name": "Materialien", "visible": 1,
            "contents": [
                {
                    "filename": "aufgabe.txt", "filesize": 3, "type": "file",
                    "filepath": "/Woche1/", "fileurl": "https://m/w1/aufgabe.txt",
                },
                {
                    "filename": "aufgabe.txt", "filesize": 3, "type": "file",
                    "filepath": "/Woche2/", "fileurl": "https://m/w2/aufgabe.txt",
                },
            ],
        }],
    }]
    client = _StubClient(sections, [], course)

    manifest = await download_course(client, 1, tmp_path, "https://moodle.example")

    module_dir = manifest.course_dir / "Kurse" / "S1" / "Infotexte" / "Materialien"
    att_dir = module_dir / "Anhänge"
    assert (att_dir / "Woche1" / "aufgabe.txt").exists()
    assert (att_dir / "Woche2" / "aufgabe.txt").exists()
    assert len(manifest.downloaded) == 2


# ---------------------------------------------------------------- inline images
def _empty_manifest(tmp_path: Path) -> DownloadManifest:
    return DownloadManifest(
        course_id=1,
        course_name="T",
        course_dir=tmp_path,
        kurs_md_path=tmp_path / "Kurs.md",
    )


def test_save_inline_images_writes_base64_payload(tmp_path: Path) -> None:
    """Regression for the actual course-40794 bug: assignment intros that
    embed lesson content as ``<img src="data:image/png;base64,...">`` must
    have those images materialized as files inside Anhänge/."""
    module = {"id": 1, "modname": "assign", "name": "A"}
    assign_meta = {
        "intro": f'<h3>Lesen</h3><img src="data:image/png;base64,{_PNG_B64}"/>',
    }
    manifest = _empty_manifest(tmp_path)

    paths, digest_to_path = _save_inline_images(module, assign_meta, tmp_path, manifest)

    assert len(paths) == 1
    saved = paths[0]
    assert saved.parent.name == "Anhänge"
    assert saved.name == f"bild-{_PNG_DIGEST}.png"
    assert saved.read_bytes() == _PNG_BYTES
    assert digest_to_path[_PNG_DIGEST] == saved
    assert manifest.downloaded == [saved]
    assert manifest.total_bytes == len(_PNG_BYTES)


def test_save_inline_images_dedups_by_digest(tmp_path: Path) -> None:
    module = {"id": 1, "modname": "assign", "name": "A"}
    # Same payload twice in the intro
    duplicated = f'<img src="data:image/png;base64,{_PNG_B64}"/>' * 2
    assign_meta = {"intro": duplicated}
    manifest = _empty_manifest(tmp_path)

    paths, digest_to_path = _save_inline_images(module, assign_meta, tmp_path, manifest)
    assert len(paths) == 1
    assert len(digest_to_path) == 1
    # Only one write counted in the manifest.
    assert len(manifest.downloaded) == 1


def test_save_inline_images_returns_empty_when_no_data_uris(tmp_path: Path) -> None:
    module = {"id": 1, "modname": "assign", "name": "A"}
    assign_meta = {"intro": "<p>only text, no images</p>"}
    paths, mapping = _save_inline_images(module, assign_meta, tmp_path, _empty_manifest(tmp_path))
    assert paths == []
    assert mapping == {}
    # Anhänge folder must not be created when there's nothing to save.
    assert not (tmp_path / "Anhänge").exists()


def test_save_inline_images_is_idempotent(tmp_path: Path) -> None:
    """Second run with the same bytes already on disk should be a no-op."""
    module = {"id": 1, "modname": "assign", "name": "A"}
    assign_meta = {"intro": f'<img src="data:image/png;base64,{_PNG_B64}"/>'}
    m1 = _empty_manifest(tmp_path)
    _save_inline_images(module, assign_meta, tmp_path, m1)
    assert len(m1.downloaded) == 1

    m2 = _empty_manifest(tmp_path)
    _save_inline_images(module, assign_meta, tmp_path, m2)
    assert m2.downloaded == []
    assert len(m2.skipped) == 1
