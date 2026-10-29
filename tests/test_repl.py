"""Tests for live mode's LiveSession - which side each command runs on, and
what's copied across - against a fake websocket client, so no Home
Assistant is involved."""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant_repl.local_session import LocalSession
from homeassistant_repl.repl import Captured, LiveSession, is_quit_call


class FakeClient:
    """Records exec calls; `variables` stands in for the server session's
    own namespace as ha_repl_server/sessions would report it."""

    def __init__(self, variables: list[str] | None = None) -> None:
        self.executed: list[str] = []
        self.variables = variables or ["hass", "obj", "sql", "hass_api"]
        self.fail_on: str | None = None
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.release = asyncio.Event()
        self.release.set()

    async def send(self, type_: str, **payload: Any) -> None:
        self.sent.append((type_, payload))

    async def call(self, type_: str, **payload: Any) -> Any:
        if type_ == "ha_repl_server/sessions":
            return {"sessions": [{"name": "default", "variables": self.variables}]}
        assert type_ == "ha_repl_server/exec"
        self.executed.append(payload["code"])
        await self.release.wait()
        error = None
        if self.fail_on and self.fail_on in payload["code"]:
            error = {"traceback": "ImportError: nope\n"}
        return {
            "stdout": "",
            "value": None,
            "error": error,
            "duration": 0.0,
            "truncated": False,
        }


def _live(client: FakeClient, **bindings: Any) -> LiveSession:
    return LiveSession(
        client,
        "default",
        LocalSession(dict(bindings)),
        shared=frozenset(bindings),
    )


async def test_plain_python_runs_locally_and_never_reaches_the_server():
    client = FakeClient()
    live = _live(client)

    await live.run("import json\nx = json.dumps([1, 2])")

    assert client.executed == []
    assert live.local.globals_["x"] == "[1, 2]"


async def test_help_on_something_in_home_assistant_runs_there():
    # `help` is bound locally too, but isn't a local function being called
    # on a Home Assistant value - each side has its own.
    client = FakeClient()
    live = _live(client)

    assert await live.run("help(hass.states)")

    assert client.executed == ["help(hass.states)"]


async def test_show_runs_on_the_side_its_argument_is_on(capsys):
    client = FakeClient()
    live = _live(client)

    assert await live.run("show(hass.config)")
    assert client.executed == ["show(hass.config)"]

    assert await live.run("show(complex(1, 2), private=True)")
    assert client.executed == ["show(hass.config)"]
    assert "_complex__" not in capsys.readouterr().out


async def test_command_using_hass_or_obj_runs_on_the_server():
    client = FakeClient()
    live = _live(client)

    await live.run('hass.states.get("sun.sun")')
    await live.run('obj["/sun"]')

    assert client.executed == ['hass.states.get("sun.sun")', 'obj["/sun"]']


async def test_variable_left_on_the_server_routes_later_commands_there():
    client = FakeClient()
    live = _live(client)

    await live.run('s = hass.states.get("sun.sun")')
    await live.run("s.state")

    assert client.executed[-1] == "s.state"
    assert "s" not in live.local.globals_


async def test_existing_server_session_variables_are_picked_up_at_startup():
    client = FakeClient(["hass", "obj", "hass_api", "earlier"])
    live = _live(client, hass_api=None)
    await live.refresh_remote_names()

    await live.run("earlier")
    await live.run("hass_api")

    assert client.executed == ["earlier"]


async def test_a_name_lives_wherever_it_was_last_assigned():
    client = FakeClient()
    live = _live(client)

    await live.run("x = 5")
    await live.run("x = hass.config.latitude")
    assert "x" not in live.local.globals_
    await live.run("x")
    assert client.executed == ["x = hass.config.latitude", "x"]

    await live.run("x = 7")
    await live.run("x + 1")
    assert len(client.executed) == 2
    assert live.local.globals_["_"] == 8


async def test_server_side_loop_variable_does_not_take_over_a_local_name():
    client = FakeClient()
    live = _live(client)

    await live.run("s = 'mine'")
    await live.run("[s for s in hass.states.async_all()]")
    await live.run("s")

    assert live.local.globals_["_"] == "mine"


async def test_mixed_snippet_is_split_by_statement():
    client = FakeClient()
    live = _live(client)

    ok = await live.run(
        "import json\n"
        "eid = 'sun.sun'\n"
        "s = hass.states.get(eid)\n"
        "state = s.state\n"
        "local_only = json.dumps([1])\n"
    )

    assert ok
    # One preamble copying `eid` over, then each server statement on its own.
    assert client.executed == [
        "eid = 'sun.sun'",
        "s = hass.states.get(eid)",
        "state = s.state",
    ]
    assert live.local.globals_["local_only"] == "[1]"


async def test_statements_sharing_a_line_go_together():
    client = FakeClient()
    live = _live(client)

    await live.run("a = 1; hass.config")

    assert client.executed == ["a = 1; hass.config"]


async def test_run_stops_at_the_first_failure():
    client = FakeClient()
    client.fail_on = "hass.boom"
    live = _live(client)

    ok = await live.run("hass.boom\nafter = 1")

    assert not ok
    assert "after" not in live.local.globals_


async def test_capture_collects_instead_of_printing(capsys):
    client = FakeClient()
    capture = Captured()
    live = _live(client)
    live.capture = capture

    assert await live.run("print('hi')\n{'a': [1, 2]}")
    assert capsys.readouterr().out == ""
    assert (capture.stdout, capture.value, capture.error) == (
        "hi\n",
        {"a": [1, 2]},
        None,
    )

    assert not await live.run("1/0")
    assert capture.error is not None
    assert capture.error["type"] == "ZeroDivisionError"
    assert "1/0" in capture.error["traceback"]


async def test_capture_turns_values_into_json_ready_data():
    live = _live(FakeClient())
    live.capture = Captured()

    await live.run("object")
    assert live.capture.value == "<class 'object'>"


async def test_plain_local_data_is_copied_over_before_a_server_command():
    client = FakeClient()
    live = _live(client)

    await live.run('eid = "sun.sun"\nwanted = {"a": [1, 2.5, None]}')
    await live.run("hass.states.get(eid), wanted")

    assert client.executed == [
        "eid = 'sun.sun'\nwanted = {'a': [1, 2.5, None]}",
        "hass.states.get(eid), wanted",
    ]


async def test_locally_imported_module_is_imported_on_the_server_too():
    client = FakeClient()
    live = _live(client)

    await live.run("import json as j")
    await live.run("j.dumps(hass.config.as_dict())")

    assert client.executed == [
        "import json as j",
        "j.dumps(hass.config.as_dict())",
    ]


async def test_failed_copy_stops_before_the_command_itself(capsys):
    client = FakeClient()
    client.fail_on = "import json"
    live = _live(client)

    await live.run("import json")
    await live.run("json.dumps(hass.config.as_dict())")

    assert client.executed == ["import json as json"]
    assert "ImportError" in capsys.readouterr().err


async def test_uncopyable_local_value_refuses_the_command_with_a_reason(capsys):
    client = FakeClient()
    live = _live(client)

    await live.run("class Frame: pass\ndf = Frame()")
    await live.run("hass.states.get(df)")

    assert client.executed == []
    err = capsys.readouterr().err
    assert "`df`" in err and "`hass`" in err


async def test_uncopyable_local_name_rebound_by_the_command_is_ignored():
    client = FakeClient()
    live = _live(client)

    await live.run("class Frame: pass\ns = Frame()")
    await live.run("[s.entity_id for s in hass.states.async_all()]")

    assert client.executed == ["[s.entity_id for s in hass.states.async_all()]"]


async def test_annotations_are_not_values_to_copy_over(capsys):
    client = FakeClient()
    live = _live(client)

    await live.run("from typing import Any\nclass Frame: pass")
    await live.run("data: dict[str, Any] = hass.data['x']")
    await live.run("def first(e: Frame) -> Frame:\n    return hass.states.get(e)")

    assert client.executed == [
        "data: dict[str, Any] = hass.data['x']",
        "def first(e: Frame) -> Frame:\n    return hass.states.get(e)",
    ]
    assert capsys.readouterr().err == ""


async def test_a_name_only_in_an_annotation_does_not_choose_the_side():
    client = FakeClient()
    live = _live(client)

    await live.run("entry = hass.config_entries.async_entries('x')[0]")
    await live.run("count: entry = 3")

    assert client.executed == ["entry = hass.config_entries.async_entries('x')[0]"]
    assert live.local.globals_["count"] == 3


async def test_a_bare_declaration_leaves_a_name_where_it_was():
    client = FakeClient()
    live = _live(client)

    # as a plugin declares the shell's own names for a type checker
    await live.run("TYPE_CHECKING = False")
    await live.run("if TYPE_CHECKING:\n    hass: object\n    obj: object\nhass.states")

    assert client.executed == ["hass.states"]


async def test_statement_mixing_a_local_only_binding_with_hass_is_refused(capsys):
    client = FakeClient()
    live = _live(client, sql=object(), hass_api=None)

    assert not await live.run("hass, sql, hass_api")

    assert client.executed == []
    err = capsys.readouterr().err
    assert "`hass_api`, `sql` only work locally and `hass` only inside" in err


async def test_plain_data_assigned_on_the_server_comes_back_as_local():
    class Answering(FakeClient):
        async def call(self, type_: str, **payload: Any) -> Any:
            result = await super().call(type_, **payload)
            if type_ == "ha_repl_server/exec" and "names" in payload["code"]:
                assert payload["fetch"] == ["names", "state"]
                result["names"] = {"names": ["sun.sun", {"t": "tuple", "v": [1]}]}
            return result

    client = Answering()
    live = _live(client)

    await live.run("names = obj.find_names(); state = hass.states.get(names[0])")
    await live.run("first = names[0]")

    # `names` came back, so using it is a local matter; `state` didn't.
    assert live.local.globals_["names"] == ["sun.sun", (1,)]
    assert live.local.globals_["first"] == "sun.sun"
    assert "names" not in live.remote_names
    assert "state" in live.remote_names
    assert len(client.executed) == 1


async def test_underscore_follows_whichever_side_answered_last():
    client = FakeClient()
    live = _live(client)

    await live.run("1 + 1")
    assert live.local.globals_["_"] == 2
    await live.run("_")
    assert client.executed == []

    await live.run("hass.config.latitude")
    await live.run("_")
    assert client.executed == ["hass.config.latitude", "_"]


async def test_syntax_error_is_reported_locally(capsys):
    client = FakeClient()
    live = _live(client)

    await live.run("hass.(")

    assert client.executed == []
    assert "SyntaxError" in capsys.readouterr().err


def test_is_quit_call():
    assert is_quit_call("quit()")
    assert is_quit_call("exit()")
    assert not is_quit_call("quit(1)")
    assert not is_quit_call("x = quit()")


async def test_local_sql_result_crosses_to_the_server_as_its_rows():
    import io

    import nanoarrow as na
    from nanoarrow.ipc import StreamWriter

    from homeassistant_repl.sql import SqlResult

    column = na.array(["sun.sun", "zone.home"], na.string())
    batch = na.c_array_from_buffers(
        na.struct({"entity_id": column.schema}),
        length=2,
        buffers=[],
        children=[column],
    )
    buf = io.BytesIO()
    with StreamWriter.from_writable(buf) as writer:
        writer.write_stream(batch)

    client = FakeClient()
    live = _live(client)
    live.local.globals_["r"] = SqlResult.from_arrow(buf.getvalue())

    await live.run("[hass.states.get(row[0]) for row in r]")

    assert client.executed == [
        "r = [['sun.sun'], ['zone.home']]",
        "[hass.states.get(row[0]) for row in r]",
    ]
    # Still the real result locally.
    assert isinstance(live.local.globals_["r"], SqlResult)


async def test_capture_takes_a_server_value_as_data_and_drops_error_frames():
    class Answering(FakeClient):
        async def call(self, type_: str, **payload: Any) -> Any:
            result = await super().call(type_, **payload)
            if type_ == "ha_repl_server/exec":
                result["value"] = "text form"
                result["value_tree"] = {"t": "dict", "v": [["a", [1, 2]]]}
                result["error"] = {"type": "E", "traceback": "E\n", "stacks": [{}]}
            return result

    live = _live(Answering())
    live.capture = Captured()

    await live.run("hass.states")

    assert live.capture.value == {"a": [1, 2]}
    assert live.capture.error == {"type": "E", "traceback": "E\n"}


async def test_capture_takes_what_show_made_as_its_text():
    class Answering(FakeClient):
        async def call(self, type_: str, **payload: Any) -> Any:
            result = await super().call(type_, **payload)
            if type_ == "ha_repl_server/exec":
                result["value"] = "Config(\n    country='GB',\n)"
                result["value_tree"] = {"t": "obj", "n": "Config", "f": []}
                result["shown"] = True
            return result

    live = _live(Answering())
    live.capture = Captured()

    await live.run("show(hass.config)")

    assert live.capture.value == "Config(\n    country='GB',\n)"


async def test_local_auto_await_does_not_rewrite_class_bodies():
    local = LocalSession({})
    code = (
        "import asyncio, dataclasses\n"
        "@dataclasses.dataclass\n"
        "class P:\n"
        "    xs: list = dataclasses.field(default_factory=list)\n"
        "    async def twice(self, n=int('2')):\n"
        "        return asyncio.sleep(0, result=n * 2)\n"
        "[P().xs, P().twice()]"
    )

    assert await local.evaluate(code) == [[], 4]


async def test_iterator_that_came_back_is_copied_over_without_being_used_up():
    client = FakeClient()
    live = _live(client)
    live.local.globals_["names"] = iter(["a", "b"])

    await live.run("[hass.states.get(n) for n in names]")

    assert client.executed == [
        "names = iter(['a', 'b'])",
        "[hass.states.get(n) for n in names]",
    ]
    assert list(live.local.globals_["names"]) == ["a", "b"]


async def test_interrupt_asks_the_server_to_stop_only_while_it_runs_something():
    client = FakeClient()
    live = _live(client)
    assert not live.interrupt()  # nothing running there

    client.release.clear()
    running = asyncio.ensure_future(live.run("hass.states"))
    await asyncio.sleep(0)
    assert live.interrupt()
    await asyncio.sleep(0)
    assert client.sent == [("ha_repl_server/cancel", {"session": "default"})]
    client.release.set()
    await running
    assert not live.interrupt()
