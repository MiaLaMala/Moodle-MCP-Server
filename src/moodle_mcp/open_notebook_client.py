"""Thin async client for a self-hosted Open Notebook instance.

Open Notebook (https://github.com/lfnovo/open-notebook) is a self-hosted
NotebookLM-style RAG notebook (FastAPI + SurrealDB) with a plain REST API.
We push locally-synced Moodle content into it as *Sources* so it can be
studied/queried there directly, instead of only living as local Markdown.

Auth: Open Notebook's ``PasswordAuthMiddleware`` only requires an
``Authorization: Bearer <password>`` header when the server itself has
``OPEN_NOTEBOOK_PASSWORD`` set server-side — if that env var is unset there,
auth is disabled entirely and the header is simply ignored. We always send
it when a password is configured on our side; nothing breaks if the server
doesn't need it.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx


logger = logging.getLogger("moodle_mcp.open_notebook")


class OpenNotebookError(RuntimeError):
    """Raised when the Open Notebook API returns an error or an unexpected shape."""


class OpenNotebookClient:
    def __init__(
        self,
        base_url: str,
        password: Optional[str] = None,
        timeout: float = 60.0,
    ) -> None:
        headers = {"User-Agent": "moodle-mcp/0.3"}
        if password:
            headers["Authorization"] = f"Bearer {password}"
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout, headers=headers
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "OpenNotebookClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as err:
            raise OpenNotebookError(
                f"Netzwerkfehler bei {method} {path}: {err}"
            ) from err

        if response.status_code >= 400:
            raise OpenNotebookError(
                f"{method} {path} -> HTTP {response.status_code}: {response.text[:300]}"
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    # ------------------------------------------------------------------ notebooks
    async def list_notebooks(self) -> list[dict[str, Any]]:
        result = await self._request("GET", "/notebooks")
        return result if isinstance(result, list) else []

    async def find_notebook_by_name(self, name: str) -> Optional[dict[str, Any]]:
        for notebook in await self.list_notebooks():
            if notebook.get("name") == name:
                return notebook
        return None

    async def create_notebook(self, name: str, description: str = "") -> dict[str, Any]:
        result = await self._request(
            "POST", "/notebooks", json={"name": name, "description": description}
        )
        if not isinstance(result, dict):
            raise OpenNotebookError(
                f"Unerwartete Antwort beim Anlegen von Notebook {name!r}: {result}"
            )
        return result

    async def get_or_create_notebook(self, name: str, description: str = "") -> dict[str, Any]:
        """Look the notebook up by name first — avoids creating duplicates
        on every push (Open Notebook has no "get or create" endpoint of its own)."""
        existing = await self.find_notebook_by_name(name)
        if existing is not None:
            return existing
        return await self.create_notebook(name, description)

    # ------------------------------------------------------------------ sources
    async def create_text_source(
        self,
        notebook_id: str,
        title: str,
        content: str,
        topics: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Create a ``type="text"`` source via the JSON endpoint (no file upload needed).

        ``/sources/json`` is Open Notebook's JSON-body variant of the
        multipart ``/sources`` endpoint — simpler here since we're pushing
        plain markdown text, not binary files.
        """
        payload: dict[str, Any] = {
            "notebooks": [notebook_id],
            "type": "text",
            "content": content,
            "title": title,
            "transformations": [],
            "embed": True,
            "async_processing": False,
        }
        result = await self._request("POST", "/sources/json", json=payload)
        if not isinstance(result, dict):
            raise OpenNotebookError(
                f"Unerwartete Antwort beim Anlegen von Source {title!r}: {result}"
            )
        if topics:
            source_id = result.get("id")
            if source_id:
                updated = await self.update_source_topics(source_id, topics)
                if updated is not None:
                    result = updated
        return result

    async def update_source_topics(
        self, source_id: str, topics: list[str]
    ) -> Optional[dict[str, Any]]:
        result = await self._request(
            "PUT", f"/sources/{source_id}", json={"topics": topics}
        )
        return result if isinstance(result, dict) else None

    async def delete_source(self, source_id: str) -> None:
        await self._request("DELETE", f"/sources/{source_id}")
