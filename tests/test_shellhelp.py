"""Tests for `?` and `??` in the shells."""

from __future__ import annotations

from typing import Any

import pytest

from homeassistant_repl.complete import Doc, Source
from homeassistant_repl.shellhelp import answer, help_request, shell_help


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("?", ("?", "")),
        ("hass?", ("?", "hass")),
        (" hass.states ? ", ("?", "hass.states")),
        ("?hass", ("?", "hass")),
        ("hass??", ("??", "hass")),
        ("??obj", ("??", "obj")),
        ("x = 1", None),
        ("'what?'", None),
        ("?x?", None),
        ("for x in y:\n    x?", None),
    ],
)
def test_help_request(source, expected):
    assert help_request(source) == expected


def test_shell_help_is_for_the_mode():
    live = shell_help("live").plain
    api = shell_help("api").plain
    assert "hass" in live and "sql" in live
    assert "sql" not in api and "obj" in api
    assert "thing??" in api


async def _answer(
    request, located: Source | None = None, described: Doc | None = None
) -> list[str]:
    ran: list[str] = []

    async def run(code: str) -> Any:
        ran.append(code)

    async def locate(expr: str) -> Source | None:
        return located

    async def describe(expr: str) -> Doc | None:
        return described

    await answer(request, "live", run, locate, describe)
    return ran


async def test_thing_question_runs_help_where_thing_lives():
    assert await _answer(("?", "hass.states")) == ["help(hass.states)"]


async def test_nothing_is_called_to_answer(capsys):
    assert await _answer(("?", "hass.states.get('x')")) == []
    assert "would call something" in capsys.readouterr().out


async def test_thing_double_question_prints_where_its_source_is(capsys):
    found = Source(
        "homeassistant.core.HomeAssistant",
        "/usr/src/homeassistant/homeassistant/core.py",
        412,
        "https://github.com/home-assistant/core/blob/2026.11.0b0/homeassistant/core.py#L412",
    )
    assert await _answer(("??", "hass"), found) == []
    out = capsys.readouterr().out
    assert "homeassistant/core.py:412" in out
    assert "blob/2026.11.0b0/homeassistant/core.py#L412" in out
    await _answer(("??", "len"), Source("builtins.len", "", 0, ""))
    assert "built in" in capsys.readouterr().out


async def test_an_attribute_its_class_documents_is_described_not_helped(capsys):
    found = Doc(
        "sql.tables",
        "instance",
        "list[SqlTable]",
        doc="`.tables` the recorder's tables, real and virtual",
        value="12 items",
        owner="homeassistant_repl.sql.SqlTool",
    )
    assert await _answer(("?", "sql.tables"), described=found) == []
    out = capsys.readouterr().out
    assert (
        "instance of list[SqlTable], attribute of homeassistant_repl.sql.SqlTool" in out
    )
    assert "12 items" in out and "recorder's tables" in out
    # Anything else is help() as before.
    plain = Doc("x", "instance", "list")
    assert await _answer(("?", "x"), described=plain) == ["help(x)"]
