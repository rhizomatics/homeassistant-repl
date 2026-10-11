"""Tests for plugins - split into statements, run quietly on the
right side, and never fatal - against a fake websocket client, so no Home
Assistant is involved."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from homeassistant_repl.local_session import LocalSession
from homeassistant_repl.plugins import (
    bind_context,
    bind_rebind,
    local_runner,
    run_plugins,
    split_statements,
)
from homeassistant_repl.repl import Captured, LiveSession, bind_live_rebind


class FakeClient:
    """Records what was sent to the server; fails anything naming `boom`."""

    def __init__(self) -> None:
        self.executed: list[str] = []

    async def call(self, type_: str, **payload: Any) -> Any:
        assert type_ == "ha_repl_server/exec"
        self.executed.append(payload["code"])
        error = None
        if "boom" in payload["code"]:
            error = {
                "type": "KeyError",
                "message": "'boom'",
                "traceback": "KeyError: 'boom'\n",
            }
        return {
            "stdout": "from the server\n" if "print" in payload["code"] else "",
            "value": "a value" if "states" in payload["code"] else None,
            "error": error,
            "duration": 0.0,
            "truncated": False,
        }


def _live(client: FakeClient, capture: Captured | None = None) -> LiveSession:
    return LiveSession(client, "default", LocalSession({}), capture=capture)


def _file(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_split_statements_keeps_line_numbers_and_whole_definitions():
    source = (
        "import json\n"
        "\n"
        "# a comment\n"
        "@decorate\n"
        "def f():\n"
        "    return 1\n"
        "\n"
        "a = 1; b = 2\n"
    )

    assert split_statements(source) == [
        (1, "import json"),
        (4, "@decorate\ndef f():\n    return 1"),
        (8, "a = 1; b = 2"),
    ]


async def test_each_statement_runs_on_the_side_it_belongs(tmp_path):
    client = FakeClient()
    live = _live(client)
    path = _file(
        tmp_path,
        "10-helpers.py",
        "import datetime as dt\n"
        "limit = 3\n"
        "sensors = hass.data['sensor']\n"
        "def stale():\n"
        "    return hass.states.async_all()\n"
        "def double(n):\n"
        "    return n * 2\n",
    )

    loaded = await run_plugins([path], live.run_quiet)

    assert loaded == [path]
    assert client.executed == [
        "sensors = hass.data['sensor']",
        "def stale():\n    return hass.states.async_all()",
    ]
    assert live.local.globals_["limit"] == 3
    assert live.local.globals_["double"](2) == 4
    assert {"sensors", "stale"} <= live.remote_names


async def test_a_failing_statement_is_a_warning_and_the_rest_still_runs(
    tmp_path, capsys
):
    client = FakeClient()
    live = _live(client)
    first = _file(
        tmp_path,
        "10-first.py",
        "a = 1\nb = undefined_name\nc = hass.data['boom']\nd = 4\n",
    )
    second = _file(tmp_path, "20-second.py", "e = 5\n")

    loaded = await run_plugins([first, second], live.run_quiet)

    assert loaded == [first, second]
    assert {k: live.local.globals_[k] for k in "ade"} == {"a": 1, "d": 4, "e": 5}
    captured = capsys.readouterr()
    assert captured.out == ""
    warnings = captured.err.splitlines()
    assert len(warnings) == 2
    assert warnings[0].startswith("ha-repl: plugin: ")
    assert warnings[0].endswith(
        "10-first.py:2: NameError: name 'undefined_name' is not defined"
    )
    assert warnings[1].endswith("10-first.py:3: KeyError: 'boom'")


async def test_a_file_that_does_not_parse_is_skipped_with_a_warning(tmp_path, capsys):
    live = _live(FakeClient())
    bad = _file(tmp_path, "10-bad.py", "x = 1\ndef (:\n")
    good = _file(tmp_path, "20-good.py", "y = 2\n")

    loaded = await run_plugins([bad, good], live.run_quiet)

    assert loaded == [good]
    assert "x" not in live.local.globals_
    assert live.local.globals_["y"] == 2
    assert "10-bad.py:2: SyntaxError" in capsys.readouterr().err


async def test_nothing_is_echoed_and_prints_go_to_stderr(tmp_path, capsys):
    client = FakeClient()
    live = _live(client)
    path = _file(
        tmp_path,
        "10-noisy.py",
        "1 + 1\nprint('from here')\nhass.states\nprint(hass)\n",
    )

    await run_plugins([path], live.run_quiet)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "from here\nfrom the server\n"
    assert "_" not in live.local.globals_


async def test_json_capture_is_left_untouched_by_plugin_files(tmp_path, capsys):
    # `ha-repl --json exec`: the snippet's result is all the JSON holds.
    capture = Captured()
    live = _live(FakeClient(), capture)
    path = _file(tmp_path, "10-noisy.py", "print('hello')\nhass.data['boom']\n")

    await run_plugins([path], live.run_quiet)

    assert live.capture is capture
    assert capture == Captured()
    assert "KeyError: 'boom'" in capsys.readouterr().err


async def test_api_mode_runs_locally_and_only_lines_using_hass_fail(tmp_path, capsys):
    session = LocalSession({"obj": object()})
    path = _file(
        tmp_path,
        "20-bookmarks.py",
        "sensors = hass.data['sensor']\n"
        "def stale():\n"
        "    return hass.states.async_all()\n"
        "print('loaded')\n"
        "'not echoed'\n",
    )

    loaded = await run_plugins([path], local_runner(session))

    assert loaded == [path]
    assert callable(session.globals_["stale"])  # `hass` only looked up when called
    assert "sensors" not in session.globals_
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "20-bookmarks.py:1: NameError: name 'hass' is not defined" in captured.err
    assert "loaded\n" in captured.err
    assert "not echoed" not in captured.err


MODE_AWARE = (
    'if MODE == "live":\n'
    "    sensors = hass.data['sensor']\n"
    "else:\n"
    "    sensors = obj.find(domain='sensor')\n"
    "where = (MODE, SERVER)\n"
)


async def test_a_file_can_branch_on_mode_in_api_mode(tmp_path, capsys):
    session = LocalSession({"obj": _Obj()})
    bind_context(session.globals_, "api", None)
    path = _file(tmp_path, "10-mode.py", MODE_AWARE)

    await run_plugins([path], local_runner(session))

    assert session.globals_["sensors"] == ["found sensor"]
    assert session.globals_["where"] == ("api", None)
    assert capsys.readouterr().err == ""


async def test_a_file_can_branch_on_mode_in_live_mode(tmp_path, capsys):
    client = FakeClient()
    live = _live(client)
    bind_context(live.local.globals_, "live", "house")
    path = _file(tmp_path, "10-mode.py", MODE_AWARE)

    await run_plugins([path], live.run_quiet)

    # MODE goes over with the statement that needs it, as plain data
    assert client.executed == [
        "MODE = 'live'",
        MODE_AWARE.rsplit("where", 1)[0].rstrip(),
    ]
    assert live.local.globals_["where"] == ("live", "house")
    assert capsys.readouterr().err == ""


class _Obj:
    def find(self, domain: str) -> list[str]:
        return [f"found {domain}"]


async def test_rebind_puts_back_the_shells_own_names_in_live_mode(tmp_path, capsys):
    client = FakeClient()
    sql = object()
    live = LiveSession(
        client, "default", LocalSession({"sql": sql}), shared=frozenset({"sql"})
    )
    bind_context(live.local.globals_, "live", "house")
    path = _file(tmp_path, "10-helpers.py", "limit = 3\n")
    bind_live_rebind(live, [path])
    await run_plugins([path], live.run_quiet)
    show = live.local.globals_["show"]

    # each of these is a local variable now, in the way of the real thing
    assert await live.run("hass = obj = sql = show = MODE = 1; limit = 99; mine = 5")
    assert await live.run("hass")
    assert client.executed == []

    assert await live.run("rebind()")

    assert live.local.globals_["sql"] is sql
    assert live.local.globals_["show"] is show
    assert live.local.globals_["MODE"] == "live"
    assert "hass" not in live.local.globals_
    assert "obj" not in live.local.globals_
    # the plugins ran again, and nothing else was touched
    assert live.local.globals_["limit"] == 3
    assert live.local.globals_["mine"] == 5
    # so `hass` is Home Assistant's again
    assert await live.run("hass.states")
    assert client.executed == ["hass.states"]
    capsys.readouterr()


async def test_rebind_leaves_the_plugins_alone_when_told_to(tmp_path):
    live = _live(FakeClient())
    path = _file(tmp_path, "10-helpers.py", "limit = 3\n")
    bind_live_rebind(live, [path])
    await run_plugins([path], live.run_quiet)

    assert await live.run("limit = 99; show = 1; rebind(plugins=False)")

    assert live.local.globals_["limit"] == 99
    assert callable(live.local.globals_["show"])


async def test_rebind_in_api_mode_puts_obj_back(tmp_path):
    obj = _Obj()
    session = LocalSession({"obj": obj})
    bind_context(session.globals_, "api", None)
    path = _file(tmp_path, "10-helpers.py", "limit = 3\n")
    bind_rebind(session.globals_, [path], local_runner(session))

    assert await session.run("obj = None; limit = 99; rebind()")

    assert session.globals_["obj"] is obj
    assert session.globals_["limit"] == 3


async def test_a_plugin_that_calls_rebind_does_not_run_them_all_again(tmp_path):
    runs: list[int] = []
    session = LocalSession({"runs": runs})
    path = _file(tmp_path, "10-loop.py", "runs.append(1)\nrebind()\n")
    bind_rebind(session.globals_, [path], local_runner(session))

    assert await session.run("rebind()")

    assert runs == [1]
