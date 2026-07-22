"""Async Moodle Web Services client.

Handles token exchange via ``/login/token.php``, persists the token to a
local cache, and wraps the two REST calls needed by the MCP tools:
``core_enrol_get_users_courses`` and ``core_course_get_contents`` (plus
``mod_assign_get_assignments`` for enriching assign modules with duedates).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from pathlib import Path
from typing import Any, Optional

import httpx

from .config import MoodleConfig


logger = logging.getLogger("moodle_mcp.client")


class MoodleAuthError(RuntimeError):
    """Raised when Moodle authentication cannot succeed."""


class MoodleAPIError(RuntimeError):
    """Raised when a Web Services call returns an exception payload."""


class _TransientError(RuntimeError):
    """Internal marker for retryable failures (timeouts, 5xx, 429).

    Never escapes the client — callers only ever see :class:`MoodleAPIError`.
    Kept separate from the 401/403 re-auth retry in :meth:`MoodleClient._ws_call`,
    which addresses an unrelated failure mode (expired token, not an
    overloaded/flaky server).
    """


_TRANSIENT_STATUS = {429, 500, 502, 503, 504}


async def _sleep_backoff(base: float, attempt: int) -> None:
    """Exponential backoff with jitter: ``base * 2**attempt + U(0, base)``."""
    delay = base * (2**attempt) + random.uniform(0, base)
    await asyncio.sleep(delay)


def _looks_like_mobile_service_disabled(payload: dict[str, Any]) -> bool:
    error = (payload.get("error") or "").lower()
    errorcode = (payload.get("errorcode") or "").lower()
    hints = (
        "web service",
        "webservice",
        "service not available",
        "mobile service",
        "enablewebservices",
    )
    return any(hint in error for hint in hints) or errorcode in {
        "enablewsdescription",
        "webserviceisnotenabled",
    }


class MoodleClient:
    """Thin async wrapper over the Moodle Web Services REST API."""

    def __init__(self, config: MoodleConfig) -> None:
        self.config = config
        self._token: Optional[str] = config.token or None
        self._userid: Optional[int] = None
        self._http = httpx.AsyncClient(
            timeout=config.timeout,
            headers={"User-Agent": "moodle-mcp/0.1"},
        )

        if not self._token:
            self._token = self._load_cached_token()

    # ------------------------------------------------------------------ lifecycle
    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "MoodleClient":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    # ------------------------------------------------------------------ token cache
    def _load_cached_token(self) -> Optional[str]:
        path = self.config.token_cache
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

        if data.get("url") != self.config.url:
            logger.info("Token cache URL mismatch, ignoring cache")
            return None
        token = data.get("token")
        return token if isinstance(token, str) and token else None

    def _save_cached_token(self, token: str) -> None:
        path = self.config.token_cache
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"url": self.config.url, "token": token}),
                encoding="utf-8",
            )
        except OSError as err:
            logger.warning("Could not persist token cache to %s: %s", path, err)

    def _invalidate_cache(self) -> None:
        try:
            self.config.token_cache.unlink(missing_ok=True)
        except OSError:
            pass

    # ------------------------------------------------------------------ auth
    async def _exchange_token(self) -> str:
        """Exchange username/password for a Web Services token.

        Raises:
            MoodleAuthError: bad credentials or mobile service disabled.
        """
        if not (self.config.username and self.config.password):
            raise MoodleAuthError(
                "Kein gültiges Token im Cache und keine MOODLE_USERNAME/"
                "MOODLE_PASSWORD in der Konfiguration — kann kein Token holen."
            )

        url = f"{self.config.url}/login/token.php"
        data = {
            "username": self.config.username,
            "password": self.config.password,
            "service": "moodle_mobile_app",
        }
        try:
            response = await self._http.post(url, data=data)
        except httpx.HTTPError as err:
            raise MoodleAuthError(f"Netzwerkfehler beim Token-Austausch: {err}") from err

        if response.status_code == 404:
            raise MoodleAuthError(
                "/login/token.php ist nicht erreichbar (HTTP 404). Der Moodle "
                "Mobile Web Service scheint deaktiviert zu sein — bitte einen "
                "Admin-Token beschaffen und als MOODLE_TOKEN setzen."
            )

        try:
            payload = response.json()
        except ValueError as err:
            raise MoodleAuthError(
                f"Unerwartete Antwort von /login/token.php (kein JSON): "
                f"{response.text[:200]}"
            ) from err

        if "token" in payload and payload["token"]:
            token = str(payload["token"])
            self._save_cached_token(token)
            return token

        if _looks_like_mobile_service_disabled(payload):
            raise MoodleAuthError(
                "Der Moodle Mobile Web Service ist auf dieser Instanz "
                "deaktiviert. Bitte beim Moodle-Admin einen persönlichen "
                "Web-Service-Token besorgen und als MOODLE_TOKEN setzen. "
                f"(Detail: {payload.get('error') or payload})"
            )

        raise MoodleAuthError(
            f"Token-Austausch fehlgeschlagen: {payload.get('error') or payload}"
        )

    async def _ensure_token(self) -> str:
        if self._token:
            return self._token
        self._token = await self._exchange_token()
        return self._token

    # ------------------------------------------------------------------ retry/backoff
    async def _post_with_retry(self, url: str, data: dict[str, Any]) -> httpx.Response:
        """POST with exponential-backoff retries for transient failures.

        Retries network errors (timeouts, connection resets) and HTTP
        429/5xx responses up to ``config.retry_max_attempts`` times.
        Anything else (including 401/403, handled separately by the
        caller's re-auth logic) is returned/raised immediately.
        """
        attempts = max(1, self.config.retry_max_attempts)
        last_err: Optional[Exception] = None
        for attempt in range(attempts):
            try:
                response = await self._http.post(url, data=data)
            except httpx.HTTPError as err:
                last_err = err
                if attempt + 1 >= attempts:
                    raise MoodleAPIError(f"Netzwerkfehler: {err}") from err
                logger.info(
                    "Transienter Netzwerkfehler (Versuch %s/%s): %s",
                    attempt + 1, attempts, err,
                )
                await _sleep_backoff(self.config.retry_backoff_base, attempt)
                continue

            if response.status_code in _TRANSIENT_STATUS and attempt + 1 < attempts:
                logger.info(
                    "Transienter HTTP %s (Versuch %s/%s), retry...",
                    response.status_code, attempt + 1, attempts,
                )
                await _sleep_backoff(self.config.retry_backoff_base, attempt)
                continue
            return response

        # Unreachable in practice (loop always returns or raises above), but
        # keeps mypy/pyright happy about the return type.
        raise MoodleAPIError(f"Netzwerkfehler: {last_err}")

    # ------------------------------------------------------------------ WS core
    async def _ws_call(
        self,
        function: str,
        params: Optional[dict[str, Any]] = None,
        _retry: bool = True,
    ) -> Any:
        token = await self._ensure_token()
        url = f"{self.config.url}/webservice/rest/server.php"
        full_params: dict[str, Any] = {
            "wstoken": token,
            "wsfunction": function,
            "moodlewsrestformat": "json",
        }
        if params:
            full_params.update(params)

        response = await self._post_with_retry(url, full_params)

        if response.status_code in (401, 403):
            if _retry:
                logger.info("WS call got %s, reauthenticating", response.status_code)
                self._token = None
                self._invalidate_cache()
                return await self._ws_call(function, params, _retry=False)
            raise MoodleAPIError(
                f"{function}: HTTP {response.status_code} trotz frischem Token."
            )

        try:
            payload = response.json()
        except ValueError as err:
            raise MoodleAPIError(
                f"{function}: keine gültige JSON-Antwort ({response.text[:200]})"
            ) from err

        if isinstance(payload, dict) and payload.get("exception"):
            errorcode = payload.get("errorcode", "")
            if errorcode in {"invalidtoken", "accessexception"} and _retry:
                logger.info("WS call errored with %s, reauthenticating", errorcode)
                self._token = None
                self._invalidate_cache()
                return await self._ws_call(function, params, _retry=False)
            raise MoodleAPIError(
                f"{function} fehlgeschlagen: "
                f"{payload.get('message') or payload.get('errorcode') or payload}"
            )

        return payload

    # ------------------------------------------------------------------ public
    async def get_site_info(self) -> dict[str, Any]:
        info = await self._ws_call("core_webservice_get_site_info")
        if isinstance(info, dict) and "userid" in info:
            self._userid = int(info["userid"])
        return info  # type: ignore[return-value]

    async def list_courses(self) -> list[dict[str, Any]]:
        if self._userid is None:
            await self.get_site_info()
        assert self._userid is not None
        result = await self._ws_call(
            "core_enrol_get_users_courses",
            {"userid": self._userid},
        )
        return result if isinstance(result, list) else []

    async def get_course_contents(self, course_id: int) -> list[dict[str, Any]]:
        result = await self._ws_call(
            "core_course_get_contents",
            {"courseid": course_id},
        )
        return result if isinstance(result, list) else []

    async def get_assignments(self, course_id: int) -> list[dict[str, Any]]:
        """Fetch assignment metadata (duedate, intro) for a course.

        Returns an empty list if the call fails — this is an enrichment step,
        not critical for core functionality.
        """
        try:
            result = await self._ws_call(
                "mod_assign_get_assignments",
                {"courseids[0]": course_id},
            )
        except MoodleAPIError as err:
            logger.warning("mod_assign_get_assignments failed: %s", err)
            return []

        if not isinstance(result, dict):
            return []
        courses = result.get("courses") or []
        for course in courses:
            if course.get("id") == course_id:
                return course.get("assignments") or []
        return []

    # ------------------------------------------------------------------ categories
    async def get_category_name(self, category_id: int) -> Optional[str]:
        """Resolve a category id to its name.

        Returns ``None`` when the category cannot be fetched (permissions,
        missing id, etc.) — callers should fall back to a generic folder.
        """
        try:
            result = await self._ws_call(
                "core_course_get_categories",
                {
                    "criteria[0][key]": "id",
                    "criteria[0][value]": category_id,
                },
            )
        except MoodleAPIError as err:
            logger.warning("core_course_get_categories failed for id=%s: %s", category_id, err)
            return None
        if isinstance(result, list) and result:
            first = result[0]
            if isinstance(first, dict):
                name = first.get("name")
                if isinstance(name, str) and name.strip():
                    return name.strip()
        return None

    # ------------------------------------------------------------------ quizzes
    async def get_quizzes_by_courses(self, course_ids: list[int]) -> list[dict[str, Any]]:
        """List quiz activities for the given courses via ``mod_quiz_get_quizzes_by_courses``.

        Returns an empty list on failure (missing capability, quiz module
        disabled, …) — quiz coverage is an enrichment, not core functionality.
        """
        params: dict[str, Any] = {}
        for i, cid in enumerate(course_ids):
            params[f"courseids[{i}]"] = cid
        try:
            result = await self._ws_call("mod_quiz_get_quizzes_by_courses", params)
        except MoodleAPIError as err:
            logger.warning("mod_quiz_get_quizzes_by_courses failed: %s", err)
            return []
        if not isinstance(result, dict):
            return []
        return result.get("quizzes") or []

    async def get_quiz_user_attempts(self, quiz_id: int) -> list[dict[str, Any]]:
        """List the current user's own attempts at a quiz.

        Uses ``status="all"`` so finished, in-progress, and abandoned
        attempts are all visible — the caller decides which to review.
        """
        try:
            result = await self._ws_call(
                "mod_quiz_get_user_attempts",
                {"quizid": quiz_id, "status": "all", "includepreviews": 0},
            )
        except MoodleAPIError as err:
            logger.warning("mod_quiz_get_user_attempts failed for quiz=%s: %s", quiz_id, err)
            return []
        if not isinstance(result, dict):
            return []
        return result.get("attempts") or []

    async def get_quiz_attempt_review(self, attempt_id: int) -> dict[str, Any]:
        """Fetch the rendered review (question + correct answer + state) for one attempt.

        Requires the ``mod/quiz:reviewmyattempts`` capability (own attempts,
        available since Moodle 3.1). Returns ``{}`` on failure so the
        caller can skip that attempt instead of aborting the whole sync.
        """
        try:
            result = await self._ws_call(
                "mod_quiz_get_attempt_review",
                {"attemptid": attempt_id, "page": -1},
            )
        except MoodleAPIError as err:
            logger.warning(
                "mod_quiz_get_attempt_review failed for attempt=%s: %s", attempt_id, err
            )
            return {}
        return result if isinstance(result, dict) else {}

    # ------------------------------------------------------------------ books
    async def get_books_by_courses(self, course_ids: list[int]) -> list[dict[str, Any]]:
        """List ``mod_book`` instances (with chapters) for the given courses.

        ``core_course_get_contents`` does not expose book chapter text, so
        this dedicated call is needed for full book coverage.
        """
        params: dict[str, Any] = {}
        for i, cid in enumerate(course_ids):
            params[f"courseids[{i}]"] = cid
        try:
            result = await self._ws_call("mod_book_get_books_by_courses", params)
        except MoodleAPIError as err:
            logger.warning("mod_book_get_books_by_courses failed: %s", err)
            return []
        if not isinstance(result, dict):
            return []
        return result.get("books") or []

    async def get_book_chapters(self, book_id: int) -> list[dict[str, Any]]:
        """Fetch chapter title + HTML content for one book."""
        try:
            result = await self._ws_call(
                "mod_book_get_chapters",
                {"bookid": book_id},
            )
        except MoodleAPIError as err:
            logger.warning("mod_book_get_chapters failed for book=%s: %s", book_id, err)
            return []
        if not isinstance(result, dict):
            return []
        return result.get("chapters") or []

    # ------------------------------------------------------------------ forums
    async def get_forums_by_courses(self, course_ids: list[int]) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        for i, cid in enumerate(course_ids):
            params[f"courseids[{i}]"] = cid
        try:
            result = await self._ws_call("mod_forum_get_forums_by_courses", params)
        except MoodleAPIError as err:
            logger.warning("mod_forum_get_forums_by_courses failed: %s", err)
            return []
        return result if isinstance(result, list) else []

    async def get_forum_discussions(self, forum_id: int) -> list[dict[str, Any]]:
        try:
            result = await self._ws_call(
                "mod_forum_get_forum_discussions",
                {"forumid": forum_id},
            )
        except MoodleAPIError as err:
            logger.warning("mod_forum_get_forum_discussions failed for forum=%s: %s", forum_id, err)
            return []
        if not isinstance(result, dict):
            return []
        return result.get("discussions") or []

    async def get_forum_discussion_posts(self, discussion_id: int) -> list[dict[str, Any]]:
        try:
            result = await self._ws_call(
                "mod_forum_get_forum_discussion_posts",
                {"discussionid": discussion_id},
            )
        except MoodleAPIError as err:
            logger.warning(
                "mod_forum_get_forum_discussion_posts failed for discussion=%s: %s",
                discussion_id, err,
            )
            return []
        if not isinstance(result, dict):
            return []
        return result.get("posts") or []

    # ------------------------------------------------------------------ file download
    async def download_file(self, file_url: str, dest_path: "Path") -> int:
        """Stream-download a Moodle file (pluginfile.php URL) to ``dest_path``.

        Appends the Web Services token as a query parameter. Creates parent
        directories as needed. Returns the number of bytes written.

        Transient failures (timeouts, connection errors, 429/5xx) are
        retried with exponential backoff (``config.retry_max_attempts``);
        each retry re-downloads the whole file since ``dest_path`` is
        reopened in truncating mode, so partial writes never corrupt the
        result.
        """
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        attempts = max(1, self.config.retry_max_attempts)
        for attempt in range(attempts):
            try:
                return await self._download_once(file_url, dest_path)
            except _TransientError as err:
                if attempt + 1 >= attempts:
                    raise MoodleAPIError(
                        f"Datei-Download fehlgeschlagen ({file_url}): {err}"
                    ) from err
                logger.info(
                    "Transienter Fehler beim Download von %s (Versuch %s/%s): %s",
                    file_url, attempt + 1, attempts, err,
                )
                await _sleep_backoff(self.config.retry_backoff_base, attempt)
        raise MoodleAPIError(f"Datei-Download fehlgeschlagen ({file_url})")

    async def _download_once(self, file_url: str, dest_path: Path) -> int:
        token = await self._ensure_token()
        separator = "&" if "?" in file_url else "?"
        url = f"{file_url}{separator}token={token}"

        bytes_written = 0
        try:
            async with self._http.stream("GET", url) as response:
                if response.status_code in (401, 403):
                    # Try one re-auth pass (unrelated to the transient-retry loop).
                    self._token = None
                    self._invalidate_cache()
                    token = await self._ensure_token()
                    retry_url = f"{file_url}{separator}token={token}"
                    async with self._http.stream("GET", retry_url) as retry:
                        retry.raise_for_status()
                        with dest_path.open("wb") as fh:
                            async for chunk in retry.aiter_bytes():
                                fh.write(chunk)
                                bytes_written += len(chunk)
                    return bytes_written

                if response.status_code in _TRANSIENT_STATUS:
                    raise _TransientError(f"HTTP {response.status_code}")
                response.raise_for_status()
                with dest_path.open("wb") as fh:
                    async for chunk in response.aiter_bytes():
                        fh.write(chunk)
                        bytes_written += len(chunk)
        except httpx.HTTPStatusError as err:
            raise MoodleAPIError(f"Datei-Download fehlgeschlagen ({file_url}): {err}") from err
        except httpx.HTTPError as err:
            raise _TransientError(str(err)) from err
        return bytes_written

    # ------------------------------------------------------------------ submissions
    async def get_submission_status(self, assign_id: int) -> dict[str, Any]:
        result = await self._ws_call(
            "mod_assign_get_submission_status",
            {"assignid": assign_id},
        )
        return result if isinstance(result, dict) else {}

    async def upload_file(self, file_path: "Path", itemid: int = 0) -> int:
        """Upload a local file to Moodle's user draft area.

        Passing ``itemid=0`` creates a new draft area and returns its id.
        Re-using that id for subsequent uploads groups the files into a
        single submission draft.
        """
        token = await self._ensure_token()
        url = f"{self.config.url}/webservice/upload.php"
        try:
            with file_path.open("rb") as fh:
                files = {"file_box": (file_path.name, fh)}
                data = {"token": token, "filearea": "draft", "itemid": str(itemid)}
                response = await self._http.post(url, data=data, files=files)
        except OSError as err:
            raise MoodleAPIError(f"Konnte Datei nicht öffnen: {err}") from err
        except httpx.HTTPError as err:
            raise MoodleAPIError(f"Upload fehlgeschlagen: {err}") from err

        try:
            payload = response.json()
        except ValueError as err:
            raise MoodleAPIError(f"Upload-Antwort war kein JSON: {response.text[:200]}") from err

        if isinstance(payload, dict) and payload.get("error"):
            raise MoodleAPIError(f"Upload abgelehnt: {payload.get('error')}")
        if not isinstance(payload, list) or not payload:
            raise MoodleAPIError(f"Upload gab unerwartete Antwort zurück: {payload}")

        itemid = payload[0].get("itemid")
        if itemid is None:
            raise MoodleAPIError(f"Upload-Antwort enthielt kein itemid: {payload[0]}")
        return int(itemid)

    async def save_submission(
        self,
        assign_id: int,
        online_text_html: Optional[str] = None,
        file_itemid: Optional[int] = None,
    ) -> Any:
        """Save/update an assignment submission as a draft.

        At least one of ``online_text_html`` or ``file_itemid`` must be given.
        """
        params: dict[str, Any] = {"assignmentid": assign_id}
        if online_text_html is not None:
            params["plugindata[onlinetext_editor][text]"] = online_text_html
            params["plugindata[onlinetext_editor][format]"] = 1  # FORMAT_HTML
            params["plugindata[onlinetext_editor][itemid]"] = 0
        if file_itemid is not None:
            params["plugindata[files_filemanager]"] = file_itemid

        if len(params) == 1:
            raise ValueError("save_submission: benötigt Text oder eine hochgeladene Datei")

        return await self._ws_call("mod_assign_save_submission", params)

    async def submit_for_grading(self, assign_id: int) -> Any:
        return await self._ws_call(
            "mod_assign_submit_for_grading",
            {"assignmentid": assign_id, "acceptsubmissionstatement": 1},
        )
