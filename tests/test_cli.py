"""Tests for print_result() - how an exec result from the server is shown -
and `ha-repl inspect`, against a fake server."""

from __future__ import annotations

import argparse
import json
from typing import Any

import pytest

from homeassistant_repl.cli import _inspect, print_result


def _base_result(**overrides) -> dict:
    base = {
        "stdout": "",
        "value": None,
        "error": None,
        "duration": 0.0,
        "truncated": False,
    }
    base.update(overrides)
    return base


def test_print_result_prints_value(capsys):
    print_result(_base_result(value="42"))

    assert capsys.readouterr().out == "42\n"


def test_print_result_shows_a_sql_result_as_its_repr_not_a_table(capsys):
    result = _base_result(value="<SqlResult 2 rows x 2 cols [a, b]>")

    print_result(result)

    assert capsys.readouterr().out == "<SqlResult 2 rows x 2 cols [a, b]>\n"


def test_print_result_pretty_prints_a_value_sent_as_a_tree(capsys):
    tree = {"t": "dict", "v": [["a", {"t": "repr", "r": "<state sun.sun=above>"}]]}

    print_result(_base_result(value="ignored", value_tree=tree))

    assert capsys.readouterr().out == "{'a': <state sun.sun=above>}\n"


def test_print_result_draws_a_traceback_from_the_frames_sent(capsys):
    error = {
        "type": "ZeroDivisionError",
        "message": "division by zero",
        "traceback": "plain text, unused\n",
        "stacks": [
            {
                "type": "ZeroDivisionError",
                "message": "division by zero",
                "is_cause": False,
                "notes": [],
                "hidden": 0,
                "frames": [
                    {
                        "file": "/usr/src/homeassistant/homeassistant/core.py",
                        "line": 12,
                        "name": "boom",
                        "first": 11,
                        "source": ["def boom():\n", "    return 1 / 0\n"],
                        "position": [12, 11, 12, 16],
                    }
                ],
            }
        ],
    }

    print_result(_base_result(error=error))

    err = capsys.readouterr().err
    assert "core.py:12 in boom" in err
    assert "12 " in err and "return 1 / 0" in err
    assert "ZeroDivisionError: division by zero" in err
    assert "plain text" not in err


def test_print_result_falls_back_to_the_plain_traceback(capsys):
    error = {"type": "SyntaxError", "message": "m", "traceback": "SyntaxError: m\n"}

    print_result(_base_result(error=error))

    assert capsys.readouterr().err == "SyntaxError: m\n"


class _InspectClient:
    """Enough of a live server for `ha-repl inspect`."""

    url = "ws://example/api/websocket"
    token = "token"

    async def call(self, type_: str, **payload: Any) -> Any:
        if type_ == "ha_repl_server/sessions":
            return {"sessions": []}
        if type_ == "ha_repl_server/info":
            return {"version": "0.14.0", "commands": ["complete"]}
        if type_ == "ha_repl_server/sql_tables":
            return {"tables": []}
        assert type_ == "ha_repl_server/complete"
        if payload["kind"] == "doc":
            return {
                "doc": {
                    "name": payload["source"],
                    "kind": "function",
                    "type": "homeassistant.core.StateMachine.async_set",
                    "signature": "(entity_id: str) -> None",
                    "doc": "Set the state of an entity.",
                }
            }
        return {
            "source": {
                "name": "homeassistant.core.StateMachine.async_set",
                "file": "/usr/src/homeassistant/homeassistant/core.py",
                "line": 1,
                "url": "https://github.com/home-assistant/core/blob/2026.11.0b0/homeassistant/core.py#L1",
            }
        }


@pytest.fixture(autouse=True)
def _no_rest_client(monkeypatch):
    """A live session's `hass_api` is a REST client, never needed here."""

    async def no_client(url: str, token: str) -> None:
        return None

    monkeypatch.setattr("homeassistant_repl.repl.hass_api", no_client)


def _inspect_args(**overrides) -> argparse.Namespace:
    return argparse.Namespace(**{
        "session": "default",
        "auto_await": True,
        "server_name": None,
        "plugins": [],
        "json": False,
        "expression": "hass.states.async_set",
        "source": False,
        **overrides,
    })


async def test_inspect_describes_and_with_source_says_where(capsys):
    assert await _inspect(_InspectClient(), _inspect_args(source=True)) == 0  # type: ignore[arg-type]
    out = capsys.readouterr().out
    assert "hass.states.async_set(entity_id: str) -> None" in out
    assert "Set the state of an entity." in out
    assert "blob/2026.11.0b0/homeassistant/core.py#L1" in out


async def test_inspect_json_has_both(capsys):
    assert await _inspect(_InspectClient(), _inspect_args(json=True)) == 0  # type: ignore[arg-type]
    result = json.loads(capsys.readouterr().out)
    assert result["doc"]["signature"] == "(entity_id: str) -> None"
    assert result["source"]["line"] == 1


async def test_inspect_something_unknown_fails(capsys):
    args = _inspect_args(expression="nothing_here")
    assert await _inspect(_InspectClient(), args) == 1  # type: ignore[arg-type]
    assert "can't tell" in capsys.readouterr().err
