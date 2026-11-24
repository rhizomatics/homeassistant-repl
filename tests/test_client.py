"""Tests for resolve_url()/resolve_token()'s env var/.env precedence, run
without Home Assistant."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import niquests
import pytest

from homeassistant_repl.client import (
    Client,
    HaReplError,
    _dotenv,
    client_version,
    resolve_token,
    resolve_url,
)


@pytest.fixture(autouse=True)
def _clear_dotenv_cache():
    """_dotenv() is process-cached; each test gets its own cwd, so the cache
    must be dropped too or an earlier test's .env (or lack of one) leaks in."""
    _dotenv.cache_clear()
    yield
    _dotenv.cache_clear()


@pytest.fixture(autouse=True)
def _no_real_env(monkeypatch):
    monkeypatch.delenv("HASS_SERVER", raising=False)
    monkeypatch.delenv("HASS_TOKEN", raising=False)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)


def _write_dotenv(tmp_path: Path, monkeypatch, text: str) -> None:
    (tmp_path / ".env").write_text(text, encoding="utf-8")
    monkeypatch.chdir(tmp_path)


def test_resolve_token_falls_back_to_dotenv(tmp_path, monkeypatch):
    _write_dotenv(tmp_path, monkeypatch, "HASS_TOKEN=from-dotenv\n")
    assert resolve_token(None) == "from-dotenv"


def test_resolve_token_real_env_beats_dotenv(tmp_path, monkeypatch):
    _write_dotenv(tmp_path, monkeypatch, "HASS_TOKEN=from-dotenv\n")
    monkeypatch.setenv("HASS_TOKEN", "from-env")
    assert resolve_token(None) == "from-env"


def test_resolve_token_explicit_arg_beats_everything(tmp_path, monkeypatch):
    _write_dotenv(tmp_path, monkeypatch, "HASS_TOKEN=from-dotenv\n")
    monkeypatch.setenv("HASS_TOKEN", "from-env")
    assert resolve_token("from-arg") == "from-arg"


def test_resolve_token_no_dotenv_file_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(HaReplError, match="No access token"):
        resolve_token(None)


def test_resolve_url_falls_back_to_dotenv(tmp_path, monkeypatch):
    _write_dotenv(tmp_path, monkeypatch, "HASS_SERVER=http://dotenv.example:8123\n")
    assert resolve_url(None) == "ws://dotenv.example:8123/api/websocket"


def test_resolve_url_real_env_beats_dotenv(tmp_path, monkeypatch):
    _write_dotenv(tmp_path, monkeypatch, "HASS_SERVER=http://dotenv.example:8123\n")
    monkeypatch.setenv("HASS_SERVER", "http://env.example:8123")
    assert resolve_url(None) == "ws://env.example:8123/api/websocket"


def test_resolve_url_no_dotenv_file_uses_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert resolve_url(None) == "ws://homeassistant.local:8123/api/websocket"


def test_dotenv_ignores_comments_blank_lines_export_and_quotes(tmp_path, monkeypatch):
    _write_dotenv(
        tmp_path,
        monkeypatch,
        """
        # a comment
        export HASS_TOKEN="quoted-token"

        HASS_SERVER='http://quoted.example:8123'
        """.replace("        ", ""),
    )
    assert resolve_token(None) == "quoted-token"
    assert resolve_url(None) == "ws://quoted.example:8123/api/websocket"


def test_client_version_reads_installed_package_version():
    # Installed via this checkout (uv sync), so this should be the real
    # thing, not the "dev" not-installed fallback.
    assert client_version() != "dev"
    assert client_version()[0].isdigit()


class _ClosedSocket:
    """The websocket extension once Home Assistant has gone away."""

    async def send_payload(self, payload: str) -> None:
        raise niquests.exceptions.ConnectionError("The HTTP extension is closed")

    async def close(self) -> None:
        raise niquests.exceptions.ConnectionError("The HTTP extension is closed")


class _Session:
    async def close(self) -> None:
        pass


async def test_a_lost_connection_is_a_ha_repl_error_and_still_closes(monkeypatch):
    client = Client("ws://example/api/websocket", "token")
    client._ws, client._session = _ClosedSocket(), _Session()

    async def unreachable() -> Client:
        raise HaReplError("Cannot connect to ws://example/api/websocket")

    monkeypatch.setattr(client, "open", unreachable)
    with pytest.raises(HaReplError, match="Cannot connect"):
        await client.call("ha_repl_server/info")
    with pytest.raises(HaReplError, match="Connection closed"):
        await client.send("ha_repl_server/cancel")
    await client.close()


class _Socket:
    """A websocket extension that answers each command with `answer`, or,
    once `dead`, fails as one Home Assistant has stopped answering on."""

    def __init__(self, answer: Any = "ok", *, dead: bool = False) -> None:
        self.answer = answer
        self.dead = dead
        self.sent: list[dict[str, Any]] = []
        self._replies: list[dict[str, Any]] = []

    async def send_payload(self, payload: str) -> None:
        msg = json.loads(payload)
        self.sent.append(msg)
        if msg["type"] == "ping":
            self._replies.append({"id": msg["id"], "type": "pong"})
        else:
            reply = {"id": msg["id"], "type": "result", "success": True}
            self._replies.append({**reply, "result": self.answer})

    async def next_payload(self) -> str:
        if self.dead:
            raise niquests.exceptions.ConnectionError(
                ConnectionResetError("Connection lost")
            )
        return json.dumps(self._replies.pop(0))

    async def close(self) -> None:
        pass


def _reconnecting_client(monkeypatch, *sockets: _Socket) -> Client:
    """A Client on the first of `sockets`, each reconnect() moving to the next."""
    client = Client("ws://example/api/websocket", "token")
    client._ws, client._session = sockets[0], _Session()
    later = iter(sockets[1:])

    async def open_() -> Client:
        client._ws, client._session = next(later), _Session()
        return client

    monkeypatch.setattr(client, "open", open_)
    return client


async def test_a_lost_connection_is_made_again_and_a_read_asked_again(monkeypatch):
    fresh = _Socket("tables")
    client = _reconnecting_client(monkeypatch, _Socket(dead=True), fresh)

    assert await client.call("ha_repl_server/sql_tables") == "tables"
    assert client.reconnects == 1
    assert [m["type"] for m in fresh.sent] == ["ha_repl_server/sql_tables"]


async def test_exec_is_not_sent_twice_when_it_may_have_run(monkeypatch):
    fresh = _Socket()
    client = _reconnecting_client(monkeypatch, _Socket(dead=True), fresh)
    client._heard = time.monotonic()  # just heard from: no ping first

    with pytest.raises(HaReplError, match="may or may not have finished"):
        await client.call("ha_repl_server/exec", code="x = 1")
    assert client.reconnects == 1
    assert fresh.sent == []


async def test_exec_after_a_quiet_spell_finds_the_lost_connection_first(monkeypatch):
    stale, fresh = _Socket(dead=True), _Socket("ran")
    client = _reconnecting_client(monkeypatch, stale, fresh)

    assert await client.call("ha_repl_server/exec", code="x = 1") == "ran"
    assert [m["type"] for m in stale.sent] == ["ping"]
    assert [m["type"] for m in fresh.sent] == ["ping", "ha_repl_server/exec"]


async def test_exec_soon_after_another_needs_no_ping(monkeypatch):
    socket = _Socket()
    client = _reconnecting_client(monkeypatch, socket)

    await client.call("ha_repl_server/exec", code="x = 1")
    await client.call("ha_repl_server/exec", code="x = 2")

    assert [m["type"] for m in socket.sent] == ["ping", *["ha_repl_server/exec"] * 2]


async def test_a_command_that_failed_is_not_asked_again(monkeypatch):
    class Refusing(_Socket):
        async def send_payload(self, payload: str) -> None:
            msg = json.loads(payload)
            self.sent.append(msg)
            self._replies.append({
                "id": msg["id"],
                "type": "result",
                "success": False,
                "error": {},
            })

    socket = Refusing()
    client = _reconnecting_client(monkeypatch, socket)

    with pytest.raises(HaReplError, match="sql failed"):
        await client.call("ha_repl_server/sql", query="x")
    assert client.reconnects == 0
    assert len(socket.sent) == 1
