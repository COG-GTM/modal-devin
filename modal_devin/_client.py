"""Typed boundary around the Devin Outposts HTTP API."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Protocol, TypeAlias, cast

from modal_devin._exceptions import OutpostsAPIError, OutpostsProtocolError

JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]
_MAX_RESPONSE_BYTES = 1024 * 1024


class HTTPResponse(Protocol):
    def __enter__(self) -> HTTPResponse: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def read(self, size: int = -1) -> bytes: ...


class UrlOpen(Protocol):
    def __call__(self, request: urllib.request.Request, *, timeout: float) -> HTTPResponse: ...


_DEFAULT_URLOPEN = cast(UrlOpen, urllib.request.urlopen)


class ClaimConflict(OutpostsAPIError):
    """The session was claimed by another worker first."""


class _HTTPError(OutpostsAPIError):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class Claim:
    deadline: str | None


class SessionStatus(StrEnum):
    """Session states understood by the Outposts worker protocol."""

    NEW = "new"
    PENDING = "pending"
    CLAIMED = "claimed"
    RUNNING = "running"
    RESUMING = "resuming"
    SUSPENDED = "suspended"
    EXIT = "exit"
    ERROR = "error"
    TERMINATED = "terminated"


def _json_object(body: bytes, *, url: str) -> JsonObject:
    if not body:
        return {}
    try:
        parsed = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OutpostsProtocolError(f"invalid JSON response from {url}") from error
    if not isinstance(parsed, dict):
        raise OutpostsProtocolError(
            f"expected a JSON object from {url}, got {type(parsed).__name__}"
        )
    return cast(JsonObject, parsed)


def _items(response: Mapping[str, JsonValue]) -> Iterable[Mapping[str, JsonValue]]:
    items = response.get("items")
    if not isinstance(items, list):
        raise OutpostsProtocolError("Outposts response is missing an items array")
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise OutpostsProtocolError(
                f"expected items[{index}] to be an object, got {type(item).__name__}"
            )
        yield item


def _nested_string(mapping: Mapping[str, JsonValue], outer_key: str, inner_key: str) -> str | None:
    outer = mapping.get(outer_key)
    if not isinstance(outer, dict):
        return None
    value = outer.get(inner_key)
    return value if isinstance(value, str) else None


def _required_nested_string(
    mapping: Mapping[str, JsonValue],
    outer_key: str,
    inner_key: str,
    *,
    context: str,
) -> str:
    value = _nested_string(mapping, outer_key, inner_key)
    if value is None:
        raise OutpostsProtocolError(f"{context} is missing {outer_key}.{inner_key}")
    return value


def _session_status(value: str) -> SessionStatus:
    try:
        return SessionStatus(value)
    except ValueError as error:
        raise OutpostsProtocolError(f"unknown Outposts session status: {value!r}") from error


class OutpostsClient:
    """Small synchronous client used by the worker runtime."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float,
        urlopen: UrlOpen = _DEFAULT_URLOPEN,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._urlopen = urlopen

    def _request(
        self,
        method: str,
        path: str,
        body: Mapping[str, JsonValue] | None = None,
    ) -> JsonObject:
        data = json.dumps(body).encode() if body is not None else None
        url = f"{self.base_url}/{path.lstrip('/')}"
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Accept", "application/json")
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("User-Agent", "modal-devin")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self._urlopen(request, timeout=self.timeout) as response:
                response_body = response.read(_MAX_RESPONSE_BYTES + 1)
                if len(response_body) > _MAX_RESPONSE_BYTES:
                    raise OutpostsProtocolError(f"response from {url} exceeded 1 MiB")
                return _json_object(response_body, url=url)
        except urllib.error.HTTPError as error:
            raise _HTTPError(error.code, f"request to {url} failed: {error}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise OutpostsAPIError(f"request to {url} failed: {error}") from error

    @staticmethod
    def _session_path(session_id: str, action: str) -> str:
        encoded = urllib.parse.quote(session_id, safe="")
        return f"/opbeta/outposts/devins/{encoded}/{action}"

    def pending_session_ids(self, pool_id: str) -> tuple[str, ...]:
        query = urllib.parse.urlencode({"pool": pool_id, "phase": "pending"})
        response = self._request("GET", f"/opbeta/outposts/devins?{query}")
        session_ids: list[str] = []
        for index, item in enumerate(_items(response)):
            session_ids.append(
                _required_nested_string(
                    item,
                    "metadata",
                    "session_id",
                    context=f"pending item {index}",
                )
            )
        return tuple(session_ids)

    def claim(self, session_id: str, acceptor_id: str) -> Claim:
        try:
            response = self._request(
                "POST",
                self._session_path(session_id, "claim"),
                {"acceptor_id": acceptor_id},
            )
        except _HTTPError as error:
            if error.status_code == 409:
                raise ClaimConflict(session_id) from error
            raise OutpostsAPIError(f"claim request for {session_id!r} failed: {error}") from error
        return Claim(deadline=_nested_string(response, "status", "claim_deadline"))

    def release(self, session_id: str, acceptor_id: str) -> None:
        self._request(
            "POST",
            self._session_path(session_id, "release"),
            {"acceptor_id": acceptor_id},
        )

    def session_status(self, session_id: str, acceptor_id: str) -> SessionStatus | None:
        query = urllib.parse.urlencode({"phase": "claimed", "acceptor_id": acceptor_id})
        response = self._request("GET", f"/opbeta/outposts/devins?{query}")
        for index, item in enumerate(_items(response)):
            item_session_id = _required_nested_string(
                item,
                "metadata",
                "session_id",
                context=f"claimed item {index}",
            )
            if item_session_id == session_id:
                value = _required_nested_string(
                    item,
                    "status",
                    "session_status",
                    context=f"claimed session {session_id!r}",
                )
                return _session_status(value)
        return None
