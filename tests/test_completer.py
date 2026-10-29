"""Tests for the shells' completer: local completion, and in live mode which
side answers - against a fake websocket client, so no Home Assistant."""

from __future__ import annotations

import asyncio
from typing import Any

from prompt_toolkit.application import create_app_session
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from homeassistant_repl import completer as completer_module
from homeassistant_repl.client import HaReplError
from homeassistant_repl.completer import HaCompleter, names_read
from homeassistant_repl.editor import python_prompt
from homeassistant_repl.local_session import LocalSession
from homeassistant_repl.repl import LiveSession

TAB = CompleteEvent(completion_requested=True)
TYPING = CompleteEvent(text_inserted=True)


class FakeClient:
    """Answers ha_repl_server/complete with `members` after `delay`."""

    def __init__(self, members: list[str] | None = None, delay: float = 0.0) -> None:
        self.members = members or ["async_set", "async_all", "get"]
        self.delay = delay
        self.asked: list[str] = []
        self.unknown = False

    async def call(self, type_: str, **payload: Any) -> Any:
        assert type_ == "ha_repl_server/complete"
        if self.unknown:
            raise HaReplError("nope", code="unknown_command")
        self.asked.append(payload["source"])
        await asyncio.sleep(self.delay)
        if payload.get("kind") == "doc":
            return {
                "doc": {
                    "name": payload["source"],
                    "kind": "instance",
                    "type": "homeassistant.core.HomeAssistant",
                }
            }
        if payload.get("kind") == "signature":
            return {
                "signature": {
                    "name": "async_set",
                    "params": [
                        {
                            "name": "entity_id",
                            "text": "entity_id: str",
                            "kind": "POSITIONAL_OR_KEYWORD",
                        },
                        {
                            "name": "force",
                            "text": "force: bool = False",
                            "kind": "KEYWORD_ONLY",
                        },
                    ],
                    "returns": "None",
                    "doc": "Set a state.",
                }
            }
        if payload["source"].startswith("nothing"):
            return {"items": [], "replace": 0, "incomplete": False, "resolved": False}
        return {
            "items": [
                {"label": m, "kind": "function", "detail": ""} for m in self.members
            ],
            "replace": 0,
            "incomplete": False,
            "resolved": True,
        }


def _live(client: FakeClient, **local: Any) -> LiveSession:
    live = LiveSession(client, "default", LocalSession(dict(local)))
    live.remote_names = {"states"}
    return live


async def texts(completer: HaCompleter, text: str, event=TAB) -> list[str]:
    return [
        c.text async for c in completer.get_completions_async(Document(text), event)
    ]


def test_names_read_copes_with_unfinished_code():
    assert names_read("for e in hass.states.async_all():\n    e.") >= {"hass", "e"}
    assert names_read('obj["/mq') == {"obj"}


async def test_local_completion_uses_the_walk_then_jedi():
    completer = HaCompleter({"data": {"alpha": 1}, "word": "x"})
    assert await texts(completer, 'data["al') == ["alpha"]
    assert "upper" in await texts(completer, "word.up")
    # A loop variable is beyond the walk: jedi follows it.
    assert "upper" in await texts(completer, "for w in [word]:\n    w.up")


async def test_while_typing_a_bare_name_needs_two_letters():
    completer = HaCompleter({"word": "x"})
    assert await texts(completer, "w", TYPING) == []
    assert await texts(completer, "wo", TYPING) == ["word"]
    assert await texts(completer, "w", TAB) != []


async def test_a_statement_reading_home_assistant_completes_there():
    client = FakeClient()
    live = _live(client, df={"a": 1})
    completer = HaCompleter(live.local.globals_, live)
    assert await texts(completer, "hass.states.async_s") == ["async_set"]
    assert client.asked == ["hass.states."]
    assert await texts(completer, "states.g") == ["get"]
    # ...and anything else here, without asking.
    assert await texts(completer, 'df["') == ["a"]
    assert len(client.asked) == 2


async def test_answers_are_kept_and_narrowed_until_the_next_command():
    client = FakeClient()
    live = _live(client)
    completer = HaCompleter(live.local.globals_, live)
    await texts(completer, "hass.states.")
    assert await texts(completer, "hass.states.async_a") == ["async_all"]
    assert client.asked == ["hass.states."]
    completer.forget()
    await texts(completer, "hass.states.")
    assert client.asked == ["hass.states."] * 2


async def test_a_slow_answer_shows_nothing_while_typing_but_is_kept(monkeypatch):
    monkeypatch.setattr(completer_module, "REMOTE_WAIT", 0.01)
    client = FakeClient(delay=0.1)
    live = _live(client)
    completer = HaCompleter(live.local.globals_, live)
    assert await texts(completer, "hass.", TYPING) == []
    await asyncio.sleep(0.15)
    assert await texts(completer, "hass.g", TYPING) == ["get"]
    assert client.asked == ["hass."]


async def test_what_home_assistant_cannot_follow_is_tried_locally():
    client = FakeClient()
    live = _live(client, nothing_here="x")
    completer = HaCompleter(live.local.globals_, live)
    # Reads `states`, so asked remotely first; unresolved there.
    assert "upper" in await texts(
        completer, "nothing_here.up if states else 1;nothing_here.up"
    )


async def test_bare_names_come_from_both_sides():
    live = _live(FakeClient(), local_thing=1)
    completer = HaCompleter(live.local.globals_, live)
    assert {"local_thing"} <= set(await texts(completer, "lo"))
    assert await texts(completer, "state") == ["states"]
    assert "hass" in await texts(completer, "ha")


async def test_an_older_server_turns_remote_completion_off():
    client = FakeClient()
    client.unknown = True
    live = _live(client)
    completer = HaCompleter(live.local.globals_, live)
    assert await texts(completer, "hass.") == []
    assert not completer.remote


async def test_tab_completes_and_enter_takes_the_completion_without_running(tmp_path):
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        prompt = python_prompt(tmp_path / "history", HaCompleter({"wordy": "x"}))
        loop = asyncio.get_running_loop()
        # Paced, since completion answers asynchronously: Tab opens the menu
        # on its one item, Tab selects it, Enter only closes the menu - so
        # the line is still there to add to - and the second Enter runs.
        steps = ((0, "wor\t"), (0.2, "\t"), (0.4, "\r"), (0.6, "+1\r"))
        for delay, typed in steps:
            loop.call_later(delay, pipe.send_text, typed)
        assert await prompt.prompt_async(">>> ") == "wordy+1"


def _shown(parts) -> tuple[str, str]:
    """The toolbar as text, and the part in bold."""
    return "".join(t for _, t in parts), "".join(
        t for style, t in parts if style.endswith("active")
    )


async def test_signature_of_a_local_call_shows_the_argument_being_typed():
    def greet(name: str, times: int = 1, *, loud: bool = False) -> str:
        """Say hello."""
        return name

    help_ = HaCompleter({"greet": greet}).signatures
    assert help_.render("greet") == []
    text, active = _shown(help_.render("greet('x', "))
    assert text == (
        "greet(name: str, times: int = 1, *, loud: bool = False) -> str  Say hello."
    )
    assert active == "times: int = 1"
    assert _shown(help_.render("greet('x', loud="))[1] == "loud: bool = False"
    assert help_.render("greet('x') ") == []


async def test_signature_of_a_call_in_home_assistant_comes_from_there():
    client = FakeClient()
    live = _live(client)
    help_ = HaCompleter(live.local.globals_, live).signatures
    # Nothing until Home Assistant answers; then it's kept, by the call.
    assert help_.render("hass.states.async_set(") == []
    await asyncio.sleep(0.01)
    text, active = _shown(help_.render("hass.states.async_set('a', force="))
    assert text.startswith("HA async_set(entity_id: str, *, force: bool = False)")
    assert active == "force: bool = False"
    assert client.asked == ["hass.states.async_set("]


async def test_the_toolbar_shows_only_inside_a_call(tmp_path):
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        prompt = python_prompt(tmp_path / "history", HaCompleter({"len": len}))
        seen = []

        def look(_):
            seen.append(prompt.bottom_toolbar is not None)

        prompt.default_buffer.on_text_changed += look
        loop = asyncio.get_running_loop()
        for delay, typed in ((0, "len("), (0.2, "[])"), (0.4, "\r")):
            loop.call_later(delay, pipe.send_text, typed)
        assert await prompt.prompt_async(">>> ") == "len([])"
        assert seen[3] is True  # after `len(`
        assert seen[-1] is False  # after the closing `)`


async def test_jedi_matches_anywhere_in_a_name_too():
    completer = HaCompleter({"word": "x"})
    # A loop variable: jedi's, still matched anywhere.
    found = await texts(completer, "for w in [word]:\n    w.case")
    assert set(found) >= {"casefold", "swapcase"}


async def test_a_long_signature_keeps_the_argument_being_typed_in_view():
    def many(alpha: int, beta: int, gamma: int, delta: int, epsilon: int) -> None:
        pass

    help_ = HaCompleter({"many": many}).signatures
    text, active = _shown(help_.render("many(1, 2, 3, 4, ", width=40))
    assert active == "epsilon: int"
    assert text.startswith("many(…")
    assert len(text) <= 40
    assert _shown(help_.render("many(", width=40))[0].endswith("…")


async def test_a_lost_connection_is_asked_about_once_not_on_every_redraw():
    class Gone(FakeClient):
        async def call(self, type_: str, **payload: Any) -> Any:
            self.asked.append(payload["source"])
            raise ConnectionError("The HTTP extension is closed")

    client = Gone()
    live = _live(client)
    completer = HaCompleter(live.local.globals_, live)
    help_ = completer.signatures
    for _ in range(3):
        assert help_.render("hass.states.async_set(") == []
        await asyncio.sleep(0)
    assert client.asked == ["hass.states.async_set("]
    assert await texts(completer, "hass.") == []
    assert await completer.locate("hass") is None


async def test_describe_asks_where_the_name_lives():
    client = FakeClient()
    live = _live(client, local_thing=[1])
    completer = HaCompleter(live.local.globals_, live)
    remote = await completer.describe("hass")
    assert remote is not None and remote.type == "homeassistant.core.HomeAssistant"
    local = await completer.describe("local_thing")
    assert local is not None and local.type == "list"
    assert client.asked == ["hass"]


async def test_ctrl_space_describes_the_name_under_the_cursor(tmp_path, capsys):
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        prompt = python_prompt(tmp_path / "history", HaCompleter({"wordy": "x"}))
        loop = asyncio.get_running_loop()
        # Ctrl-Space (NUL) with the cursor inside `wordy`, moved back two;
        # the line is kept.
        steps = ((0, "wordy\x1b[D\x1b[D"), (0.1, "\x00"), (0.4, "\x1b[C\x1b[C\r"))
        for delay, typed in steps:
            loop.call_later(delay, pipe.send_text, typed)
        assert await prompt.prompt_async(">>> ") == "wordy"
    assert "instance of str" in capsys.readouterr().out
