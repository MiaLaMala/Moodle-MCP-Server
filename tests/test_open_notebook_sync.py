"""Tests for :mod:`moodle_mcp.open_notebook_sync` — the push_to_open_notebook pipeline.

Exercises the full path from a stub Moodle client through
``download_course`` into a fake Open Notebook backend (an in-memory stub
mirroring :class:`OpenNotebookClient`'s method surface, not the real HTTP
client -- that layer is covered separately in test_open_notebook_client.py).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from moodle_mcp.config import MoodleConfig
from moodle_mcp.open_notebook_sync import push_course_to_open_notebook


class _StubMoodleClient:
    """Minimal client covering everything ``download_course`` touches."""

    def __init__(self) -> None:
        self.course = {"id": 1, "fullname": "Testkurs", "category": 1, "shortname": "TC"}
        self.sections = [{
            "name": "Fachenglisch",
            "modules": [{
                "id": 10,
                "instance": 100,
                "modname": "assign",
                "name": "Aufgabe1",
                "visible": 1,
                "contents": [],
            }],
        }]
        self.assignments = [{
            "id": 100,
            "cmid": 10,
            "intro": "<p>Text A</p>",
            "introattachments": [],
            "introfiles": [],
        }]

    async def list_courses(self) -> list[dict[str, Any]]:
        return [self.course]

    async def get_category_name(self, category_id: int) -> str:
        return "Fachinformatik"

    async def get_course_contents(self, course_id: int) -> list[dict[str, Any]]:
        return self.sections

    async def get_assignments(self, course_id: int) -> list[dict[str, Any]]:
        return self.assignments

    async def get_quizzes_by_courses(self, course_ids: list[int]) -> list[dict[str, Any]]:
        return []

    async def get_books_by_courses(self, course_ids: list[int]) -> list[dict[str, Any]]:
        return []

    async def get_forums_by_courses(self, course_ids: list[int]) -> list[dict[str, Any]]:
        return []

    async def download_file(self, file_url: str, dest_path: Path) -> int:
        return 0


class _FakeOpenNotebookClient:
    """Records calls; used in place of the real HTTP-backed client."""

    instances: list["_FakeOpenNotebookClient"] = []

    def __init__(self, base_url: str, password: str | None = None) -> None:
        self.base_url = base_url
        self.password = password
        self.notebooks: dict[str, dict[str, Any]] = {}
        self.sources: dict[str, dict[str, Any]] = {}
        self._next_id = 1
        self.create_calls: list[tuple[str, str]] = []
        self.delete_calls: list[str] = []
        type(self).instances.append(self)

    async def __aenter__(self) -> "_FakeOpenNotebookClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    async def get_or_create_notebook(self, name: str, description: str = "") -> dict[str, Any]:
        if name not in self.notebooks:
            self.notebooks[name] = {"id": f"nb-{name}", "name": name}
        return self.notebooks[name]

    async def create_text_source(
        self,
        notebook_id: str,
        title: str,
        content: str,
        topics: list[str] | None = None,
    ) -> dict[str, Any]:
        source_id = f"src-{self._next_id}"
        self._next_id += 1
        self.sources[source_id] = {
            "id": source_id,
            "title": title,
            "content": content,
            "topics": topics or [],
        }
        self.create_calls.append((title, content))
        return self.sources[source_id]

    async def delete_source(self, source_id: str) -> None:
        self.delete_calls.append(source_id)
        self.sources.pop(source_id, None)


@pytest.fixture(autouse=True)
def _patch_open_notebook_client(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeOpenNotebookClient.instances = []
    import moodle_mcp.open_notebook_sync as sync_mod

    monkeypatch.setattr(sync_mod, "OpenNotebookClient", _FakeOpenNotebookClient)


def _config(tmp_path: Path, **overrides: Any) -> MoodleConfig:
    return MoodleConfig.load(
        _env_file=None,
        url="https://lms.lernen.hamburg",
        token="tok",
        download_root=tmp_path,
        open_notebook_url="https://notebook.example.dev",
        **overrides,
    )


@pytest.mark.asyncio
async def test_push_maps_category_to_notebook_and_course_to_tag(tmp_path: Path) -> None:
    config = _config(tmp_path)
    client = _StubMoodleClient()

    summary = await push_course_to_open_notebook(client, config, course_id=1)

    fake = _FakeOpenNotebookClient.instances[-1]
    assert "Fachinformatik" in fake.notebooks
    assert fake.create_calls  # at least one source pushed
    for source in fake.sources.values():
        assert "Testkurs" in source["topics"]
    assert "Fachinformatik" in summary
    assert "Testkurs" in summary


@pytest.mark.asyncio
async def test_push_skips_kurs_and_section_overview_files(tmp_path: Path) -> None:
    config = _config(tmp_path)
    client = _StubMoodleClient()

    await push_course_to_open_notebook(client, config, course_id=1)

    fake = _FakeOpenNotebookClient.instances[-1]
    titles = [title for title, _content in fake.create_calls]
    assert not any("Kurs.md" in t or "Section.md" in t for t in titles)


@pytest.mark.asyncio
async def test_push_is_idempotent_skips_unchanged_content(tmp_path: Path) -> None:
    config = _config(tmp_path)
    client = _StubMoodleClient()

    await push_course_to_open_notebook(client, config, course_id=1)
    first_fake = _FakeOpenNotebookClient.instances[-1]
    first_push_count = len(first_fake.create_calls)
    assert first_push_count > 0

    # Second run: content unchanged -> everything should be skipped, not re-pushed.
    summary2 = await push_course_to_open_notebook(client, config, course_id=1)
    second_fake = _FakeOpenNotebookClient.instances[-1]
    assert second_fake.create_calls == []
    assert not second_fake.delete_calls
    assert "0" not in summary2 or "Neu/aktualisiert gepusht: **0**" in summary2


@pytest.mark.asyncio
async def test_push_recreates_source_when_content_changes(tmp_path: Path) -> None:
    config = _config(tmp_path)
    client = _StubMoodleClient()

    await push_course_to_open_notebook(client, config, course_id=1)
    first_fake = _FakeOpenNotebookClient.instances[-1]
    old_source_ids = set(first_fake.sources.keys())
    assert old_source_ids

    # Mutate the assignment intro so re-download produces different .md text
    # for the *same* module path (module name/dir unchanged).
    client.assignments[0]["intro"] = "<p>Text B, komplett anders</p>"

    await push_course_to_open_notebook(client, config, course_id=1)
    second_fake = _FakeOpenNotebookClient.instances[-1]
    assert second_fake.create_calls  # something got re-pushed
    assert second_fake.delete_calls  # old source(s) were cleaned up


@pytest.mark.asyncio
async def test_push_returns_error_when_open_notebook_not_configured(tmp_path: Path) -> None:
    config = MoodleConfig.load(
        _env_file=None,
        url="https://lms.lernen.hamburg",
        token="tok",
        download_root=tmp_path,
    )
    client = _StubMoodleClient()
    summary = await push_course_to_open_notebook(client, config, course_id=1)
    assert "Fehler" in summary
    assert not _FakeOpenNotebookClient.instances
