from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Mapping, Optional

import httpx

from .types import AuthResult, NyxAuthError, NyxError, NyxHTTPError, Server, UserInfo, Member, Event, FriendRequest, \
    FriendUser
from .utils import _safe_json, _safe_json_obj, get_logger
from .http_parsers import parse_user_info, parse_member, parse_friend_user, parse_friend_request


class NyxHTTPClient:
    BASE_URL = "https://nyx-app.ru/api/v1"

    def __init__(
        self,
        *,
        base_url: str | httpx.URL | None = None,
        timeout: float = 15.0,
        headers: Optional[Mapping[str, str]] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        user_agent: str = "NyxPy/0.1 (+https://github.com/kolya5544/nyxpy)",
        logger=None,
        verify: httpx._types.VerifyTypes | None = None,
        trust_env: bool = True,
    ) -> None:
        self._logger = logger or get_logger()
        self._base_url = httpx.URL(str(base_url or self.BASE_URL))
        default_headers: Dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": user_agent,
        }
        if headers:
            default_headers.update(headers)

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            headers=default_headers,
            transport=transport,
            http2=True,
            verify=verify,
            trust_env=trust_env,
        )
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._token_type: str = "Bearer"
        self.current_user: Optional[UserInfo] = None
        self._server_list: List[Server] = []
        self._member_by_user: dict[int, Member] = {}  # user_id -> Member
        self._server_members: dict[int, set[int]] = {}  # server_id -> {user_ids}

    # ---- lifecycle ----
    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "NyxHTTPClient":  # pragma: no cover
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # pragma: no cover
        await self.aclose()

    # ---- auth ----
    async def login(self, email: str, password: str) -> AuthResult:
        """POST /auth/login and store access_token for all future requests."""
        path = "/auth/login"
        payload = {"email": email, "password": password}
        self._logger.debug("POST %s payload=%s", path, _safe_json(payload))
        try:
            resp = await self._client.post(path, json=payload)
        except httpx.HTTPError as e:
            raise NyxError(f"Network error during login: {e}") from e

        if resp.status_code >= 400:
            message = self._extract_error_message(resp)
            raise NyxAuthError(resp.status_code, message, payload=_safe_json_obj(payload))

        data = self._json_or_empty(resp)
        result = self._parse_auth_response(data)
        self._apply_auth(result)  # sets Authorization for ALL subsequent requests
        if hasattr(self, "get_user_info"):
            try:
                await self.get_user_info()  # provided by high-level API subclass
            except Exception:
                pass
        self._logger.info("Authenticated as %s", email)
        return result

    def _parse_auth_response(self, data: Mapping[str, Any]) -> AuthResult:
        candidates = ("access_token", "accessToken", "token", "jwt", "session")
        access_token = None
        for k in candidates:
            v = data.get(k)
            if isinstance(v, str) and v:
                access_token = v
                break
        if not access_token and isinstance(data.get("data"), Mapping):
            nested = data["data"]
            for k in candidates:
                v = nested.get(k)
                if isinstance(v, str) and v:
                    access_token = v
                    break

        refresh_token = (
            data.get("refresh_token")
            or data.get("refreshToken")
            or (data.get("data", {}).get("refresh_token") if isinstance(data.get("data"), Mapping) else None)
        )
        token_type = data.get("token_type") or data.get("tokenType") or data.get("type") or "Bearer"

        return AuthResult(
            access_token=access_token,
            refresh_token=refresh_token if isinstance(refresh_token, str) else None,
            token_type=str(token_type) if token_type else "Bearer",
            raw=dict(data),
        )

    def _apply_auth(self, auth: AuthResult) -> None:
        if not auth.access_token:
            raise NyxAuthError(200, "Login succeeded but no token was found in response", payload=auth.raw)
        self._access_token = auth.access_token
        self._token_type = auth.token_type or "Bearer"
        self._refresh_token = auth.refresh_token
        self._client.headers["Authorization"] = f"{self._token_type} {self._access_token}"
        self.current_user = None

    @property
    def is_authenticated(self) -> bool:
        return bool(self._access_token)

    # ---- generic request helper ----
    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Mapping[str, Any]] = None,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        retry: int = 1,
        retry_backoff: float = 0.5,
    ) -> httpx.Response:
        if headers:
            merged_headers = {**self._client.headers, **headers}
        else:
            merged_headers = self._client.headers

        attempt = 0
        while True:
            try:
                resp = await self._client.request(
                    method,
                    path,
                    json=json_body,
                    params=params,
                    headers=merged_headers,
                )
            except httpx.HTTPError as e:
                self._logger.warning("Network error %r on %s %s", e, method, path)
                if attempt >= retry:
                    raise NyxError(f"Network error: {e}") from e
            else:
                if resp.status_code in (429,) or 500 <= resp.status_code < 600:
                    if attempt < retry:
                        delay = retry_backoff * (2**attempt)
                        self._logger.debug("Retrying %s %s in %.2fs (status=%s)", method, path, delay, resp.status_code)
                        await asyncio.sleep(delay)
                    else:
                        self._raise_for_status(resp)
                        return resp
                else:
                    self._raise_for_status(resp)
                    return resp
            attempt += 1

    # ---- local helpers (HTTP-only) ----
    @staticmethod
    def _json_or_empty(resp: httpx.Response) -> Dict[str, Any]:
        try:
            return resp.json()
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _extract_error_message(resp: httpx.Response) -> str:
        try:
            data = resp.json()
            return str(
                data.get("message") or data.get("error") or data.get("detail") or data.get("msg") or data
            )
        except json.JSONDecodeError:
            return resp.text or resp.reason_phrase

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if 400 <= resp.status_code:
            msg = NyxHTTPClient._extract_error_message(resp)
            if resp.status_code in (401, 403):
                raise NyxAuthError(resp.status_code, msg)
            raise NyxHTTPError(resp.status_code, msg)
