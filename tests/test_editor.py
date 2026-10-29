import asyncio
import os
import signal
import time

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from homeassistant_repl.editor import (
    is_complete,
    next_indent,
    python_prompt,
    run_interruptibly,
)
from homeassistant_repl.local_session import LocalSession

UP = "\x1b[A"


def test_a_simple_statement_is_complete():
    assert is_complete("x = 1")
    assert is_complete("await hass.async_block_till_done()")


def test_an_open_block_waits_for_an_empty_line():
    assert not is_complete("for x in range(3):")
    assert not is_complete("for x in range(3):\n    print(x)")
    assert is_complete("for x in range(3):\n    print(x)\n    ")


def test_an_open_bracket_is_incomplete():
    assert not is_complete("x = (1,")


def test_bad_code_is_complete_so_it_gets_reported():
    assert is_complete("x = = 1")


def test_next_indent():
    assert next_indent("for x in y:") == "    "
    assert next_indent("    print(x)") == "    "
    assert next_indent("    if x:") == "        "
    assert next_indent("        return x") == "    "
    assert next_indent("# note:") == ""


@pytest.fixture
def keys(tmp_path):
    """Send keys to a prompt reading `tmp_path / "history"`."""
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        prompt = python_prompt(tmp_path / "history")

        async def read(typed: str) -> str:
            pipe.send_text(typed)
            return await prompt.prompt_async(">>> ")

        yield read


async def test_a_block_is_read_and_recalled_whole(keys, tmp_path):
    block = await keys("for x in range(3):\rprint(x)\r\r")
    assert block == "for x in range(3):\n    print(x)"
    # Up brings back the whole loop as one entry; Alt-Enter runs it as is.
    assert await keys(UP + "\x1b\r") == block
    # One history entry for the whole block, not repeated when rerun unchanged.
    assert (tmp_path / "history").read_text().count(
        "+for x in range(3):\n+    print(x)"
    ) == 1


async def test_enter_mid_block_adds_a_line_instead_of_running(keys):
    await keys("x = 1\r")
    # Cursor not at the end (moved left): Enter splits the line.
    assert await keys(UP + "\x1b[D\r\x1b\r") == "x = \n1"


def _ctrl_c() -> None:
    os.kill(os.getpid(), signal.SIGINT)


async def test_ctrl_c_stops_running_code_not_the_shell(capsys):
    asyncio.get_running_loop().call_later(0.05, _ctrl_c)
    await run_interruptibly(asyncio.sleep(10))
    assert "Stopped" in capsys.readouterr().err


async def test_ctrl_c_goes_to_interrupt_first_then_cancels():
    asked = []
    done = asyncio.Event()

    def interrupt() -> bool:
        asked.append(True)
        asyncio.get_running_loop().call_later(0.05, done.set)
        return True

    # Taken care of elsewhere: run is left to finish by itself.
    asyncio.get_running_loop().call_later(0.05, _ctrl_c)
    await run_interruptibly(done.wait(), interrupt)
    assert asked == [True]

    # A second Ctrl-C, when the first didn't stop it, cancels it.
    loop = asyncio.get_running_loop()
    loop.call_later(0.05, _ctrl_c)
    loop.call_later(0.1, _ctrl_c)
    asked.clear()
    started = time.monotonic()
    await run_interruptibly(asyncio.sleep(10), lambda: asked.append(True) or True)
    assert asked == [True]
    assert time.monotonic() - started < 5


async def test_ctrl_c_on_blocking_code_interrupts_it_on_the_second_press(capsys):
    # The first press waits for the event loop, which the sleep never gives
    # back; the second raises KeyboardInterrupt inside it, reported like
    # any other error in the code.
    session = LocalSession({})
    await run_interruptibly(
        session.run(
            "import os, signal, time\n"
            "os.kill(os.getpid(), signal.SIGINT)\n"
            "os.kill(os.getpid(), signal.SIGINT)\n"
            "time.sleep(1)"
        )
    )
    assert "KeyboardInterrupt" in capsys.readouterr().err
    assert session.globals_["time"]  # the shell's session carries on


async def test_ctrl_c_handling_is_put_back_afterwards():
    before = signal.getsignal(signal.SIGINT)
    await run_interruptibly(asyncio.sleep(0))
    assert signal.getsignal(signal.SIGINT) is before
