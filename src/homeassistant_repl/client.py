"""Minimal Home Assistant websocket API client - built on niquests' websocket
extension (a GET against a ws(s):// URL upgrades in place and hands back the
extension on the response), the same HTTP stack `rest.py`'s REST client uses,
rather than pulling in the separate `websockets` library for this one job."""

from __future__ import annotations

import asyncio
import functools
import importlib.metadata
import itertools
import json
import os
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlsplit, urlunsplit

import niquests


class HaReplError(Exception):
    """Connection, auth or command failure (not an error in the user's code).

    `code`/`detail` are the server's own error code and bare message when
    this came from a failed websocket command, for a caller that wants to
    turn a specific failure into its own exception (see sql.py's SqlTool).
    """

    def __init__(
        self, message: str, *, code: str | None = None, detail: str | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@functools.cache
def client_version() -> str:
    """This package's own installed version, for the shells' startup banner
    - "dev" if run from a checkout that was never pip/uv installed, rather
    than raising over a cosmetic detail.
    """
    try:
        return importlib.metadata.version("homeassistant-repl")
    except importlib.metadata.PackageNotFoundError:
        return "dev"


@functools.cache
def _dotenv() -> dict[str, str]:
    """Best-effort KEY=VALUE pairs from a `.env` file in the current
    directory, if one exists - checked only once real environment variables
    come up empty, see resolve_url()/resolve_token(). Deliberately minimal
    rather than pulling in python-dotenv: comments, blank lines, an optional
    "export " prefix and quoted values are all a token/url file needs.
    Cached since it's read-only for the life of the process and both
    resolve_url() and resolve_token() may call it.
    """
    path = Path(".env")
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip().removeprefix("export ")
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        values[key.strip()] = value.strip().strip("'\"")
    return values


def _default_url() -> str:
    return (
        os.environ.get("HASS_SERVER")
        or _dotenv().get("HASS_SERVER")
        or "http://homeassistant.local:8123"
    )


def resolve_url(url: str | None) -> str:
    """Accept http(s)://host:8123 or ws(s)://... and return the websocket endpoint."""
    if not url:
        # Inside a Home Assistant add-on (e.g. Studio Code Server) talk via the supervisor.
        if os.environ.get("SUPERVISOR_TOKEN"):
            return "ws://supervisor/core/websocket"
        url = _default_url()
    parts = urlsplit(url)
    scheme = {"http": "ws", "https": "wss"}.get(parts.scheme, parts.scheme)
    path = parts.path.rstrip("/")
    if not path.endswith("/websocket"):
        path += "/api/websocket"
    return urlunsplit((scheme, parts.netloc, path, "", ""))


def resolve_rest_url(url: str | None) -> str:
    """Accept http(s)://host:8123, ws(s)://... or an existing Client.url, and
    return the REST API base (scheme normalised to http(s), path ending in
    `/api`) that homeassistant_api.AsyncClient expects."""
    if not url:
        if os.environ.get("SUPERVISOR_TOKEN"):
            return "http://supervisor/core/api"
        url = _default_url()
    parts = urlsplit(url)
    scheme = {"ws": "http", "wss": "https"}.get(parts.scheme, parts.scheme)
    path = parts.path.rstrip("/").removesuffix("/websocket")
    if not path.endswith("/api"):
        path += "/api"
    return urlunsplit((scheme, parts.netloc, path, "", ""))


def resolve_token(token: str | None) -> str:
    token = (
        token
        or os.environ.get("HASS_TOKEN")
        or os.environ.get("SUPERVISOR_TOKEN")
        or _dotenv().get("HASS_TOKEN")
    )
    if not token:
        raise HaReplError(
            "No access token: set HASS_TOKEN or pass --token "
            "(create one under Profile > Security > Long-lived access tokens)"
        )
    return token


class Client:
    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self._token = token
        self._ids = itertools.count(1)
        self._session: Any = None  # set by open() - AsyncSession
        self._ws: Any = (
            None  # set by open() - the extension: send_payload()/next_payload()/close()
        )
        # One command at a time: call() reads until its own reply arrives and
        # drops anything else, so two overlapping calls (e.g. a gather() of
        # two sql() queries) would each discard the other's reply.
        self._call_lock = asyncio.Lock()

    @property
    def token(self) -> str:
        """The resolved access token - also what a REST call (see rest.py's
        `hass_api()`) against the same instance should authenticate with."""
        return self._token

    async def __aenter__(self) -> Self:
        return await self.open()

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def open(self) -> Self:
        """Connect and authenticate - what `async with Client(...)` does,
        exposed directly for callers that want to keep the connection open
        past the enclosing scope (e.g. `connect()` in __init__.py)."""
        self._session = niquests.AsyncSession()
        try:
            resp = await self._session.get(self.url)
            resp.raise_for_status()
        except niquests.exceptions.RequestException as err:
            raise HaReplError(f"Cannot connect to {self.url}: {err}") from err
        if resp.extension is None:
            raise HaReplError(f"Server did not upgrade to WebSocket: {self.url}")
        self._ws = resp.extension
        msg = await self._recv()
        if msg.get("type") != "auth_required":
            raise HaReplError(f"Unexpected greeting: {msg}")
        await self._ws.send_payload(
            json.dumps({"type": "auth", "access_token": self._token})
        )
        msg = await self._recv()
        if msg.get("type") != "auth_ok":
            raise HaReplError(f"Authentication failed: {msg.get('message', msg)}")
        return self

    async def close(self) -> None:
        await self._ws.close()
        await self._session.close()

    async def _recv(self) -> dict[str, Any]:
        try:
            payload = await self._ws.next_payload()
        except niquests.exceptions.RequestException as err:
            raise HaReplError(f"Connection closed: {err}") from err
        if payload is None:
            raise HaReplError("Connection closed")
        return json.loads(payload)

    async def send(self, type_: str, **payload: Any) -> None:
        """Send a command without waiting for its reply, which call()'s own
        loop drops - for one that has to go out while a call() is still
        waiting, such as stopping the code that call() is waiting on."""
        await self._ws.send_payload(
            json.dumps({"id": next(self._ids), "type": type_, **payload})
        )

    async def call(self, type_: str, **payload: Any) -> Any:
        async with self._call_lock:
            msg_id = next(self._ids)
            await self._ws.send_payload(
                json.dumps({"id": msg_id, "type": type_, **payload})
            )
            while True:
                msg = await self._recv()
                if msg.get("id") != msg_id or msg.get("type") != "result":
                    continue
                if not msg["success"]:
                    error = msg.get("error", {})
                    code = error.get("code")
                    if code == "unknown_command":
                        raise HaReplError(
                            f"{type_} not available: is the ha_repl_server integration "
                            "installed and `ha_repl_server:` in configuration.yaml?",
                            code=code,
                        )
                    detail = error.get("message")
                    raise HaReplError(
                        f"{type_} failed: {detail or error}", code=code, detail=detail
                    )
                return msg["result"]
