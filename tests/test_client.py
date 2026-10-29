"""Tests for resolve_url()/resolve_token()'s env var/.env precedence, run
without Home Assistant."""

from __future__ import annotations

from pathlib import Path

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


async def test_a_lost_connection_is_a_ha_repl_error_and_still_closes():
    client = Client("ws://example/api/websocket", "token")
    client._ws, client._session = _ClosedSocket(), _Session()
    with pytest.raises(HaReplError, match="Connection closed"):
        await client.call("ha_repl_server/info")
    with pytest.raises(HaReplError, match="Connection closed"):
        await client.send("ha_repl_server/cancel")
    await client.close()
