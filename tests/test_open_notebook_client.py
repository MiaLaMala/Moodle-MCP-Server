"""Tests for :mod:`moodle_mcp.open_notebook_client` using ``httpx.MockTransport``.

No real Open Notebook server needed/available for this session -- these
mocks encode the documented REST shape (list/create notebooks, create text
source via /sources/json, update topics via PUT, delete via DELETE).
"""

from __future__ import annotations

import httpx
import pytest

from moodle_mcp.open_notebook_client import OpenNotebookClient, OpenNotebookError


def _client_with_transport(transport: httpx.MockTransport, password: str | None = None) -> OpenNotebookClient:
    client = OpenNotebookClient("https://notebook.example.dev", password=password)
    client._http = httpx.AsyncClient(
        base_url="https://notebook.example.dev", transport=transport
    )
    return client


@pytest.mark.asyncio
async def test_sends_bearer_header_when_password_configured() -> None:
    client = OpenNotebookClient("https://notebook.example.dev", password="secret")
    assert client._http.headers["Authorization"] == "Bearer secret"
    await client.close()


@pytest.mark.asyncio
async def test_no_auth_header_when_no_password_configured() -> None:
    client = OpenNotebookClient("https://notebook.example.dev", password=None)
    assert "Authorization" not in client._http.headers
    await client.close()


@pytest.mark.asyncio
async def test_list_notebooks_returns_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/notebooks"
        return httpx.Response(200, json=[{"id": "n1", "name": "Fachinformatik"}])

    client = _client_with_transport(httpx.MockTransport(handler))
    notebooks = await client.list_notebooks()
    assert notebooks == [{"id": "n1", "name": "Fachinformatik"}]


@pytest.mark.asyncio
async def test_find_notebook_by_name_matches_existing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"id": "n1", "name": "Fachinformatik"},
                {"id": "n2", "name": "Wirtschaft"},
            ],
        )

    client = _client_with_transport(httpx.MockTransport(handler))
    found = await client.find_notebook_by_name("Wirtschaft")
    assert found == {"id": "n2", "name": "Wirtschaft"}


@pytest.mark.asyncio
async def test_find_notebook_by_name_returns_none_when_absent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": "n1", "name": "Other"}])

    client = _client_with_transport(httpx.MockTransport(handler))
    assert await client.find_notebook_by_name("Missing") is None


@pytest.mark.asyncio
async def test_get_or_create_notebook_reuses_existing_without_posting() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": "n1", "name": "Fachinformatik"}])
        raise AssertionError("should not POST when notebook already exists")

    client = _client_with_transport(httpx.MockTransport(handler))
    notebook = await client.get_or_create_notebook("Fachinformatik")
    assert notebook == {"id": "n1", "name": "Fachinformatik"}
    assert calls == ["GET"]


@pytest.mark.asyncio
async def test_get_or_create_notebook_creates_when_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        assert request.method == "POST"
        assert request.url.path == "/notebooks"
        return httpx.Response(200, json={"id": "new-id", "name": "Neues Lernfeld"})

    client = _client_with_transport(httpx.MockTransport(handler))
    notebook = await client.get_or_create_notebook("Neues Lernfeld", description="desc")
    assert notebook == {"id": "new-id", "name": "Neues Lernfeld"}


@pytest.mark.asyncio
async def test_create_text_source_posts_to_sources_json() -> None:
    import json as _json

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/sources/json"
        payload = _json.loads(request.content)
        assert payload["notebooks"] == ["n1"]
        assert payload["type"] == "text"
        assert payload["title"] == "Kurs — Modul"
        assert payload["content"] == "Inhalt hier"
        return httpx.Response(200, json={"id": "src-1", "title": "T"})

    client = _client_with_transport(httpx.MockTransport(handler))
    source = await client.create_text_source(
        notebook_id="n1", title="Kurs — Modul", content="Inhalt hier"
    )
    assert source == {"id": "src-1", "title": "T"}


@pytest.mark.asyncio
async def test_create_text_source_follows_up_with_topics_update() -> None:
    requests_seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append((request.method, request.url.path))
        if request.url.path == "/sources/json":
            return httpx.Response(200, json={"id": "src-1", "title": "T", "topics": []})
        if request.url.path == "/sources/src-1":
            return httpx.Response(200, json={"id": "src-1", "title": "T", "topics": ["a", "b"]})
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")

    client = _client_with_transport(httpx.MockTransport(handler))
    source = await client.create_text_source(
        notebook_id="n1", title="T", content="x", topics=["a", "b"]
    )
    assert source["topics"] == ["a", "b"]
    assert requests_seen == [("POST", "/sources/json"), ("PUT", "/sources/src-1")]


@pytest.mark.asyncio
async def test_delete_source_calls_delete() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/sources/src-1"
        return httpx.Response(204)

    client = _client_with_transport(httpx.MockTransport(handler))
    await client.delete_source("src-1")  # must not raise


@pytest.mark.asyncio
async def test_error_status_raises_open_notebook_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = _client_with_transport(httpx.MockTransport(handler))
    with pytest.raises(OpenNotebookError):
        await client.list_notebooks()


@pytest.mark.asyncio
async def test_network_error_raises_open_notebook_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = _client_with_transport(httpx.MockTransport(handler))
    with pytest.raises(OpenNotebookError):
        await client.list_notebooks()


@pytest.mark.asyncio
async def test_create_notebook_raises_on_unexpected_shape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "a", "dict"])

    client = _client_with_transport(httpx.MockTransport(handler))
    with pytest.raises(OpenNotebookError):
        await client.create_notebook("X")
