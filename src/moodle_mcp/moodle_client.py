"""Async Moodle Web Services client.

Handles token exchange via ``/login/token.php``, persists the token to a
local cache, and wraps the two REST calls needed by the MCP tools:
``core_enrol_get_users_courses`` and ``core_course_get_contents`` (plus
``mod_assign_get_assignments`` for enriching assign modules with duedates).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

import httpx

from .config import MoodleConfig


logger = logging.getLogger("moodle_mcp.client")


class MoodleAuthError(RuntimeError):
    """Raised when Moodle authentication cannot succeed."""


class MoodleAPIError(RuntimeError):
    """Raised when a Web Services call returns an exception payload."""


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

        try:
            response = await self._http.post(url, data=full_params)
        except httpx.HTTPError as err:
            raise MoodleAPIError(f"Netzwerkfehler bei {function}: {err}") from err

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
